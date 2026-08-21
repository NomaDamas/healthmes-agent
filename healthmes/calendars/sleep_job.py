from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import anyio
from sqlalchemy.orm import Session, sessionmaker

from healthmes.api.auth import viewer_url
from healthmes.calendars import creds
from healthmes.calendars.approval import ApprovalCalendar, calendar_approval_target
from healthmes.calendars.base import CalendarAuthError, CalendarBackend
from healthmes.calendars.connection import CalendarBackendFence
from healthmes.calendars.jobs import _build_backend, write_source
from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    SleepObservationNoOp,
)
from healthmes.calendars.sleep_preview import preview_sleep_reconciliation
from healthmes.calendars.sleep_proposals import (
    prepare_sleep_proposal_from_observation,
)
from healthmes.calendars.sleep_reconciliation import (
    SleepCalendarReconciler,
    SleepCalendarWriteBlocked,
)
from healthmes.calendars.sleep_source import read_actual_sleep
from healthmes.config import Settings, resolve_timezone
from healthmes.mcp_server.ow_client import (
    OWClient,
    OWClientError,
    resolve_single_user_id,
)
from healthmes.store.enums import CalendarSource
from healthmes.store.session import session_scope

logger = logging.getLogger(__name__)
RECENT_SLEEP_WINDOW_DAYS = 3


class SleepReconciliationError(RuntimeError):
    pass


class SleepSummaryReader(Protocol):
    async def collect_sleep_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
    ) -> Sequence[Mapping[str, object]]: ...


def build_sleep_reconciliation_job(
    settings: Settings,
    *,
    client: SleepSummaryReader | None = None,
    backend_factory: Callable[[], CalendarBackend] | None = None,
    session_factory: sessionmaker[Session] | None = None,
    date_provider: Callable[[dt.tzinfo], dt.date] | None = None,
    connection_generation_resolver: Callable[[], str | None] | None = None,
    account_generation_resolver: Callable[[], str | None] | None = None,
) -> Callable[[], dict[str, object] | None] | None:
    calendar_source = write_source(settings)
    if calendar_source is None:
        return None
    active_client = client if client is not None else OWClient.from_settings(settings)
    resolve_generation = (
        connection_generation_resolver
        if connection_generation_resolver is not None
        else (
            (lambda: "injected")
            if backend_factory is not None
            else lambda: creds.calendar_connection_generation(
                settings,
                calendar_source,
            )
        )
    )
    backend_fence = CalendarBackendFence(
        source=calendar_source,
        backend_factory=(
            backend_factory
            if backend_factory is not None
            else lambda: _build_backend(settings, calendar_source)
        ),
        generation_resolver=resolve_generation,
    )
    resolve_account_generation = (
        account_generation_resolver
        if account_generation_resolver is not None
        else (
            None
            if backend_factory is not None
            else lambda: creds.calendar_account_generation(
                settings,
                calendar_source,
            )
        )
    )
    resolved_user_id = settings.ow_user_id
    local_timezone = resolve_timezone(settings)

    async def run_once() -> dict[str, object]:
        nonlocal resolved_user_id
        if resolved_user_id is None:
            if not isinstance(active_client, OWClient):
                raise RuntimeError("injected sleep reader requires an explicit ow_user_id")
            resolved_user_id = await resolve_single_user_id(active_client, settings)
        target_date = (
            date_provider(local_timezone)
            if date_provider is not None
            else dt.datetime.now(local_timezone).date()
        )
        return await prepare_recent_sleep_window(
            end_date=target_date,
            calendar_source=calendar_source,
            client=active_client,
            user_id=resolved_user_id,
            session_factory=session_factory,
            backend_fence=backend_fence,
            account_generation_resolver=resolve_account_generation,
            calendar_target=calendar_approval_target(
                settings,
                calendar_source,
            ),
            review_base_url=settings.public_base_url,
            review_url_builder=lambda target_date: viewer_url(
                settings,
                f"/sleep?date={target_date.isoformat()}",
            ),
        )

    def run_job() -> dict[str, object] | None:
        try:
            return anyio.run(run_once)
        except Exception as exc:
            logger.error(
                "Actual sleep reconciliation failed (%s); next interval will retry.",
                type(exc).__name__,
            )
            return None

    return run_job


async def prepare_recent_sleep_window(
    *,
    end_date: dt.date,
    calendar_source: CalendarSource,
    client: SleepSummaryReader,
    user_id: str,
    session_factory: sessionmaker[Session] | None,
    backend_fence: CalendarBackendFence,
    account_generation_resolver: Callable[[], str | None] | None,
    calendar_target: str,
    review_base_url: str | None,
    review_url_builder: Callable[[dt.date], str] | None,
) -> dict[str, object]:
    start_date = end_date - dt.timedelta(days=RECENT_SLEEP_WINDOW_DAYS - 1)
    results: list[dict[str, object]] = []
    for offset in range(RECENT_SLEEP_WINDOW_DAYS):
        target_date = start_date + dt.timedelta(days=offset)
        selected = await read_actual_sleep(
            client,
            user_id,
            target_date,
            review_base_url=review_base_url,
            review_url_builder=review_url_builder,
        )
        with session_scope(session_factory) as session:
            with backend_fence.use(session) as backend:
                account_generation = (
                    account_generation_resolver()
                    if account_generation_resolver is not None
                    else None
                )
                if (
                    account_generation_resolver is not None
                    and account_generation is None
                ):
                    raise CalendarAuthError(
                        "calendar account generation is unavailable"
                    )
                proposal = prepare_sleep_proposal_from_observation(
                    target_date=target_date,
                    calendar_source=calendar_source,
                    selected=selected,
                    session=session,
                    calendar=ApprovalCalendar(
                        backend,
                        calendar_target,
                        review_base_url,
                        review_url_builder,
                        account_generation,
                    ),
                )
            results.append(
                {
                    **proposal.snapshot,
                    "proposal_id": str(proposal.id),
                    "proposal_status": proposal.status.value,
                }
            )
    return {
        "status": "ok",
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        "results": results,
    }


async def preview_recent_sleep(
    settings: Settings,
    target_date: dt.date | None = None,
    *,
    client: SleepSummaryReader | None = None,
    backend_factory: Callable[[], CalendarBackend] | None = None,
    session_factory: sessionmaker[Session] | None = None,
    connection_generation_resolver: Callable[[], str | None] | None = None,
    account_generation_resolver: Callable[[], str | None] | None = None,
) -> dict[str, object]:
    calendar_source = write_source(settings)
    if calendar_source is None:
        raise SleepReconciliationError(
            "no configured calendar; connect Google or iCloud before dry-run"
        )
    active_client = (
        client if client is not None else OWClient.from_settings(settings)
    )
    try:
        if settings.ow_user_id is not None:
            user_id = settings.ow_user_id
        elif isinstance(active_client, OWClient):
            user_id = await resolve_single_user_id(active_client, settings)
        else:
            raise RuntimeError(
                "injected sleep reader requires an explicit ow_user_id"
            )
    except (LookupError, OWClientError) as exc:
        raise SleepReconciliationError(str(exc)) from exc
    timezone = resolve_timezone(settings)
    day = target_date or dt.datetime.now(timezone).date()
    resolve_generation = (
        connection_generation_resolver
        if connection_generation_resolver is not None
        else (
            (lambda: "injected")
            if backend_factory is not None
            else lambda: creds.calendar_connection_generation(
                settings,
                calendar_source,
            )
        )
    )
    backend_fence = CalendarBackendFence(
        source=calendar_source,
        backend_factory=(
            backend_factory
            if backend_factory is not None
            else lambda: _build_backend(settings, calendar_source)
        ),
        generation_resolver=resolve_generation,
    )
    resolve_account_generation = (
        account_generation_resolver
        if account_generation_resolver is not None
        else (
            None
            if backend_factory is not None
            else lambda: creds.calendar_account_generation(
                settings,
                calendar_source,
            )
        )
    )
    try:
        selected = await read_actual_sleep(
            active_client,
            user_id,
            day,
        )
    except OWClientError as exc:
        raise SleepReconciliationError(
            f"open-wearables sleep data unavailable ({type(exc).__name__})"
        ) from None
    with session_scope(session_factory) as session:
        with backend_fence.use(session) as backend:
            account_generation = (
                resolve_account_generation()
                if resolve_account_generation is not None
                else None
            )
            if (
                resolve_account_generation is not None
                and account_generation is None
            ):
                raise CalendarAuthError(
                    "calendar account generation is unavailable"
                )
            if isinstance(selected, SleepObservationNoOp):
                return {
                    "status": "noop",
                    "reason": selected.reason.value,
                    "calendar": calendar_source.value,
                    "local_date": day.isoformat(),
                }
            return preview_sleep_reconciliation(
                session,
                calendar_source,
                selected,
                backend,
                account_generation=account_generation,
            )


async def reconcile_recent_sleep_window(
    *,
    end_date: dt.date,
    calendar_source: CalendarSource,
    client: SleepSummaryReader,
    user_id: str,
    session_factory: sessionmaker[Session] | None,
    backend: CalendarBackend,
    account_generation: str | None = None,
) -> dict[str, object]:
    start_date = end_date - dt.timedelta(days=RECENT_SLEEP_WINDOW_DAYS - 1)
    results = []
    for offset in range(RECENT_SLEEP_WINDOW_DAYS):
        results.append(
            await reconcile_recent_sleep(
                target_date=start_date + dt.timedelta(days=offset),
                calendar_source=calendar_source,
                client=client,
                user_id=user_id,
                session_factory=session_factory,
                backend=backend,
                account_generation=account_generation,
            )
        )
    with session_scope(session_factory) as session:
        legacy_cleanup = SleepCalendarReconciler(
            session,
            backend,
            account_generation=account_generation,
        ).reconcile_legacy_history()
    return {
        "status": "ok",
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        "results": results,
        "legacy_cleanup": legacy_cleanup,
    }


async def reconcile_recent_sleep(
    *,
    target_date: dt.date,
    calendar_source: CalendarSource,
    client: SleepSummaryReader,
    user_id: str,
    session_factory: sessionmaker[Session] | None,
    backend: CalendarBackend | None,
    dry_run: bool = False,
    account_generation: str | None = None,
) -> dict[str, object]:
    selected: ActualSleepObservation | SleepObservationNoOp = await read_actual_sleep(
        client,
        user_id,
        target_date,
    )
    if isinstance(selected, SleepObservationNoOp):
        return {
            "status": "noop",
            "reason": selected.reason.value,
            "calendar": calendar_source.value,
            "local_date": target_date.isoformat(),
        }

    with session_scope(session_factory) as session:
        if dry_run and backend is None:
            raise ValueError("backend is required for an exact dry-run")
        preview = preview_sleep_reconciliation(
            session,
            calendar_source,
            selected,
            backend,
            account_generation=account_generation,
        )
        if dry_run:
            return preview
        if preview["action"] == "blocked":
            return {**preview, "status": "blocked"}
        if backend is None:
            raise ValueError("backend is required when dry_run is false")
        if backend.source is not calendar_source:
            raise ValueError("backend source does not match target calendar")
        try:
            result = SleepCalendarReconciler(
                session,
                backend,
                account_generation=account_generation,
            ).reconcile(selected)
        except SleepCalendarWriteBlocked as exc:
            return {
                **preview,
                "status": "blocked",
                "action": "blocked",
                "reason": exc.reason,
                "retryable": exc.retryable,
                "blocked_proposal_id": exc.proposal_id,
                "invalidated_schedule_proposal_ids": list(
                    exc.invalidated_proposal_ids
                ),
            }
        response: dict[str, object] = {
            **preview,
            "status": "ok",
            "action": result.action.value,
            "planned_sleep_replacements": len(result.deleted_planned_external_ids),
        }
        if result.invalidated_schedule_proposal_ids:
            response["invalidated_schedule_proposal_ids"] = list(
                result.invalidated_schedule_proposal_ids
            )
        if result.planned_sleep_cleanup_pending:
            response["status"] = "cleanup_pending"
            response["planned_sleep_cleanup_pending"] = (
                result.planned_sleep_cleanup_pending
            )
        return response
