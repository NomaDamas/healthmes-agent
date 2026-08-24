from __future__ import annotations

# noqa: SIZE_OK — confirmation, expiry, and recovery share one auditable transition state machine.
import hmac
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, tzinfo
from typing import Any

from healthmes.calendars.adjustments_logic import (
    digest_reply_handle,
    evaluate_event_eligibility,
    evaluate_health_evidence,
    initial_decision_tree,
    is_expired,
    issue_reply_handle,
    make_shorten_snapshot,
    morning_dedup_key,
    no_action_decision_tree,
    outcome_decision_tree,
    proposal_dedup_key,
    proposal_expiry,
    read_remote_event,
    redacted_receipt,
    remote_matches_snapshot,
    snapshot_matches,
)
from healthmes.calendars.adjustments_policy import _attr
from healthmes.calendars.adjustments_proposals import _http_status
from healthmes.calendars.adjustments_types import (
    TERMINAL_STATUSES,
    AdjustmentOperation,
    AdjustmentRepository,
    AdjustmentStatus,
    AmbiguousProviderResult,
    CalendarAccountGenerationChanged,
    CalendarAdjustmentWriter,
    EvaluationResult,
    ResolveResult,
    StoredAdjustmentProposal,
)
from healthmes.calendars.base import (
    CalendarAuthError,
    CalendarConflictError,
    CalendarError,
    EventNotFoundError,
    ExternalEvent,
    coerce_utc,
    ensure_utc,
)
from healthmes.store.enums import CalendarSource

_MIRROR_SNAPSHOT_UNSET = object()
_ACCOUNT_GENERATION_UNCHECKED = object()


class CalendarAdjustmentService:
    def __init__(
        self,
        repository: AdjustmentRepository,
        *,
        handle_secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._handle_secret = handle_secret
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate_morning_calendar_nudge(
        self,
        *,
        local_date: date,
        timezone: tzinfo,
        health_context: Mapping[str, Any],
        candidates: Sequence[Any],
        afternoon_busy_minutes: int,
        handle_factory: Callable[[], str] | None = None,
    ) -> EvaluationResult:
        now = ensure_utc(self._clock())
        day_key = morning_dedup_key(local_date)
        claimed = self._repository.claim_daily_evaluation(day_key, {"outcome": "evaluating"}, now)
        if not claimed:
            return EvaluationResult(outcome="deduplicated")

        eligible = [
            candidate
            for candidate in sorted(
                candidates, key=lambda item: coerce_utc(_attr(item, "start_at"))
            )
            if evaluate_event_eligibility(
                candidate,
                now=now,
                local_date=local_date,
                timezone=timezone,
                already_proposed=self._repository.has_existing_proposal(
                    proposal_dedup_key(
                        candidate, timezone=timezone, operation=AdjustmentOperation.SHORTEN
                    )
                ),
            ).eligible
        ]
        health = evaluate_health_evidence(
            health_context,
            local_date=local_date,
            now=now,
            afternoon_busy_minutes=afternoon_busy_minutes,
            eligible_event_count=len(eligible),
        )
        if not health.allowed:
            reason = health.reason or "policy_blocked"
            tree = no_action_decision_tree(reason, health.facts)
            decision_record_id = self._repository.record_no_action_evaluation(
                day_key,
                reason=reason,
                decision_tree=tree,
            )
            return EvaluationResult(
                outcome="no_action",
                reason=reason,
                decision_record_id=decision_record_id,
                decision_tree=tree,
            )

        target = eligible[0]
        snapshot = make_shorten_snapshot(target, timezone=timezone)
        decision_tree = initial_decision_tree(snapshot, health.facts)
        pair = issue_reply_handle(self._handle_secret, handle_factory=handle_factory)
        proposal = self._repository.create_pending_proposal(
            snapshot,
            handle_digest=pair.digest,
            expires_at=proposal_expiry(snapshot.original_start_at, now),
            decision_tree=decision_tree,
            now=now,
        )
        self._repository.record_daily_evaluation_outcome(
            day_key,
            {
                "outcome": "proposed",
                "proposal_id": str(proposal.id),
                "decision_record_id": str(proposal.proposal_decision_record_id),
            },
        )
        return EvaluationResult(
            outcome="proposed",
            proposal_id=proposal.id,
            reply_handle=pair.plaintext,
            expires_at=proposal.expires_at,
            decision_tree=decision_tree,
        )

    def resolve_calendar_adjustment(
        self,
        proposal_id: uuid.UUID,
        *,
        response: str,
        reply_handle: str | None,
        writer: CalendarAdjustmentWriter | None,
        response_channel: str | None = None,
        mirror_snapshot: Any = _MIRROR_SNAPSHOT_UNSET,
        current_account_generation: str | None | object = (
            _ACCOUNT_GENERATION_UNCHECKED
        ),
    ) -> ResolveResult:
        now = ensure_utc(self._clock())
        proposal = self._repository.get_proposal(proposal_id)
        if proposal is None:
            return ResolveResult(
                AdjustmentStatus.FAILED,
                redacted_receipt(status=AdjustmentStatus.FAILED, provider_code="not_found"),
            )
        if proposal.status in TERMINAL_STATUSES:
            return ResolveResult(
                proposal.status, proposal.receipt or redacted_receipt(status=proposal.status)
            )
        if not reply_handle:
            return ResolveResult(
                proposal.status,
                redacted_receipt(status=proposal.status, provider_code="missing_handle"),
            )

        digest = digest_reply_handle(reply_handle, self._handle_secret)
        if not hmac.compare_digest(proposal.reply_handle_digest, digest):
            return ResolveResult(
                proposal.status,
                redacted_receipt(status=proposal.status, provider_code="invalid_handle"),
            )

        if is_expired(proposal, now):
            return self._expire(proposal, now)

        normalized = response.strip().lower()
        if normalized in {"no", "n", "decline", "declined", "그대로"}:
            return self._terminal(
                proposal,
                AdjustmentStatus.DECLINED,
                "user_declined",
                now,
                expected_status=AdjustmentStatus.PENDING,
            )
        if normalized not in {"yes", "y", "apply", "applied", "적용"}:
            return ResolveResult(
                proposal.status,
                redacted_receipt(status=proposal.status, provider_code="unsupported_response"),
            )
        if (
            current_account_generation
            is not _ACCOUNT_GENERATION_UNCHECKED
            and current_account_generation
            != proposal.snapshot.account_generation
        ):
            return self._terminal(
                proposal,
                AdjustmentStatus.CONFLICTED,
                "calendar_account_generation_changed",
                now,
                expected_status=AdjustmentStatus.PENDING,
            )
        if writer is None:
            return self._terminal(
                proposal,
                AdjustmentStatus.FAILED_NO_CHANGE,
                "unsupported_calendar_source",
                now,
                expected_status=AdjustmentStatus.PENDING,
            )

        attempt_id = uuid.uuid4()
        applying = self._repository.compare_and_mark_applying(
            proposal.id,
            expected_digest=digest,
            now=now,
            attempt_id=attempt_id,
            response_channel=response_channel,
        )
        if applying is None:
            current = self._repository.get_proposal(proposal.id)
            current_status = current.status if current is not None else AdjustmentStatus.FAILED
            return ResolveResult(
                current_status,
                redacted_receipt(status=current_status, provider_code="not_pending"),
            )
        applying = self._repository.commit_applying_boundary(applying.id) or applying
        if mirror_snapshot is None:
            return self._terminal(
                applying,
                AdjustmentStatus.CONFLICTED,
                "mirror_deleted",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        if (
            mirror_snapshot is not _MIRROR_SNAPSHOT_UNSET
            and not snapshot_matches(applying.snapshot, mirror_snapshot)
        ):
            return self._terminal(
                applying,
                AdjustmentStatus.CONFLICTED,
                "snapshot_changed",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )

        try:
            event = writer.apply_confirmed_external_time_change(
                applying.snapshot.confirmed_change()
            )
        except CalendarAccountGenerationChanged:
            return self._terminal(
                applying,
                AdjustmentStatus.CONFLICTED,
                "calendar_account_generation_changed",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        except CalendarConflictError:
            return self._terminal(
                applying,
                AdjustmentStatus.CONFLICTED,
                "provider_412",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        except (CalendarAuthError, EventNotFoundError, ValueError):
            return self._terminal(
                applying,
                AdjustmentStatus.FAILED,
                "known_provider_failure",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        except AmbiguousProviderResult as exc:
            return self._reconcile(applying, writer, exc.provider_code, now)
        except CalendarError:
            return self._terminal(
                applying,
                AdjustmentStatus.UNKNOWN,
                "provider_200_mismatch",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK — provider SDK HTTP errors lack a common base.
            status = _http_status(exc)
            if status == 412:
                return self._terminal(
                    applying,
                    AdjustmentStatus.CONFLICTED,
                    "provider_412",
                    now,
                    expected_status=AdjustmentStatus.APPLYING,
                )
            if status in {400, 401, 403, 404, 410}:
                return self._terminal(
                    applying,
                    AdjustmentStatus.FAILED,
                    f"provider_{status}",
                    now,
                    expected_status=AdjustmentStatus.APPLYING,
                )
            if status in {429, 500, 502, 503, 504}:
                return self._reconcile(applying, writer, f"provider_{status}", now)
            return self._reconcile(applying, writer, "ambiguous", now)

        if remote_matches_snapshot(applying.snapshot, event):
            return self._terminal(
                applying,
                AdjustmentStatus.APPLIED,
                "provider_200",
                now,
                event=event,
                expected_status=AdjustmentStatus.APPLYING,
            )
        return self._terminal(
            applying,
            AdjustmentStatus.UNKNOWN,
            "provider_200_mismatch",
            now,
            expected_status=AdjustmentStatus.APPLYING,
        )

    def expire_and_reconcile_adjustments(
        self,
        writer: CalendarAdjustmentWriter | None = None,
        *,
        writer_resolver: (
            Callable[
                [CalendarSource, str | None],
                CalendarAdjustmentWriter | None,
            ]
            | None
        ) = None,
        account_generation_resolver: (
            Callable[[CalendarSource], str | None] | None
        ) = None,
    ) -> list[ResolveResult]:
        now = ensure_utc(self._clock())
        results: list[ResolveResult] = []
        for proposal in self._repository.pending_expired(now):
            results.append(self._expire(proposal, now))
        for proposal in self._repository.stale_applying(now):
            if (
                account_generation_resolver is not None
                and account_generation_resolver(
                    proposal.snapshot.calendar_source
                )
                != proposal.snapshot.account_generation
            ):
                results.append(
                    self._terminal(
                        proposal,
                        AdjustmentStatus.UNKNOWN,
                        "calendar_account_generation_changed",
                        now,
                        expected_status=AdjustmentStatus.APPLYING,
                    )
                )
                continue
            selected_writer = (
                writer_resolver(
                    proposal.snapshot.calendar_source,
                    proposal.snapshot.account_generation,
                )
                if writer_resolver is not None
                else writer
            )
            if selected_writer is None:
                results.append(
                    self._terminal(
                        proposal,
                        AdjustmentStatus.UNKNOWN,
                        "unsupported_calendar_source",
                        now,
                        expected_status=AdjustmentStatus.APPLYING,
                    )
                )
                continue
            results.append(
                self._reconcile(
                    proposal,
                    selected_writer,
                    "restart_recovery",
                    now,
                )
            )
        return results

    def _expire(self, proposal: StoredAdjustmentProposal, now: datetime) -> ResolveResult:
        return self._terminal(
            proposal,
            AdjustmentStatus.EXPIRED,
            "expired",
            now,
            expected_status=AdjustmentStatus.PENDING,
        )

    def _terminal(
        self,
        proposal: StoredAdjustmentProposal,
        status: AdjustmentStatus,
        provider_code: str,
        now: datetime,
        *,
        event: ExternalEvent | None = None,
        expected_status: AdjustmentStatus,
    ) -> ResolveResult:
        receipt = redacted_receipt(
            status=status,
            provider_code=provider_code,
            provider_event=event,
        )
        tree = outcome_decision_tree(proposal, receipt)
        terminal = self._repository.compare_and_mark_terminal(
            proposal.id,
            expected_status=expected_status,
            status=status,
            receipt=receipt,
            outcome_decision_tree=tree,
            now=now,
        )
        if terminal is None:
            current = self._repository.get_proposal(proposal.id)
            current_status = current.status if current is not None else AdjustmentStatus.FAILED
            current_receipt = (
                current.receipt
                if current is not None and current.receipt is not None
                else redacted_receipt(status=current_status, provider_code="transition_lost")
            )
            return ResolveResult(current_status, current_receipt)
        if event is not None and status in {
            AdjustmentStatus.APPLIED,
            AdjustmentStatus.APPLIED_RECOVERED,
        }:
            self._repository.update_mirror_after_apply(terminal, event)
        return ResolveResult(status, receipt, tree)

    def _reconcile(
        self,
        proposal: StoredAdjustmentProposal,
        writer: CalendarAdjustmentWriter,
        provider_code: str,
        now: datetime,
    ) -> ResolveResult:
        try:
            event = read_remote_event(
                writer,
                proposal.snapshot.external_event_id,
            )
        except CalendarAccountGenerationChanged:
            return self._terminal(
                proposal,
                AdjustmentStatus.UNKNOWN,
                "calendar_account_generation_changed",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        if event is not None and remote_matches_snapshot(proposal.snapshot, event):
            return self._terminal(
                proposal,
                AdjustmentStatus.APPLIED_RECOVERED,
                provider_code,
                now,
                event=event,
                expected_status=AdjustmentStatus.APPLYING,
            )
        return self._terminal(
            proposal,
            AdjustmentStatus.UNKNOWN,
            provider_code,
            now,
            expected_status=AdjustmentStatus.APPLYING,
        )
