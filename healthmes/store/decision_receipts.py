"""Atomic, content-minimized idempotency receipts for decision requests."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, null, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.store.models import (
    DecisionRequestReceipt,
    RetentionPolicy,
)
from healthmes.store.session import session_scope

_DECISION_RECEIPT_SCHEMA_V1 = "healthmes.decision-receipt.v1"
_DECISION_RECEIPT_SCHEMA_V2 = "healthmes.decision-receipt.v2"
_TRANSIENT_RESULT_MAX_AGE = timedelta(minutes=15)
DEFAULT_RECEIPT_MAINTENANCE_BATCH_SIZE = 256
DEFAULT_RECEIPT_MAINTENANCE_MAX_ROWS = 10_000


class DecisionReceiptConflictError(RuntimeError):
    """The same request identity was reused with different input."""


class DecisionReceiptExpiredError(RuntimeError):
    """The idempotency identity remains but its sensitive result expired."""


class DecisionReceiptOwnershipError(RuntimeError):
    """A worker no longer owns the durable request lease."""


class DecisionReceiptClaimState(StrEnum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class DecisionReceiptClaim:
    state: DecisionReceiptClaimState
    result_payload: dict[str, Any] | None = None
    expires_at: datetime | None = None
    requested_at: datetime | None = None
    lease_generation: int | None = None
    lease_expired: bool = False
    retry_after_seconds: float = 0.05


@dataclass(frozen=True, slots=True)
class DecisionReceiptCompletion:
    result_payload: dict[str, Any]
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class DecisionReceiptMaintenanceBatch:
    """One bounded scan position for startup and recurring receipt cleanup."""

    scanned: int
    next_cursor: uuid.UUID | None


class DecisionReceiptStore:
    """Coordinate one request across processes without storing its prompt."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        lease_duration: timedelta,
        retention: timedelta,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if retention <= lease_duration:
            raise ValueError("retention must exceed lease_duration")
        self._session_factory = session_factory
        self._lease_duration = lease_duration
        self._retention = retention

    def claim(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        owner_token: uuid.UUID,
        now: datetime,
        requested_at: datetime | None = None,
        legacy_fingerprint: str | None = None,
    ) -> DecisionReceiptClaim:
        current = _as_utc(now)
        initial_requested_at = _as_utc(requested_at or current)
        expired = False
        with activity_write_lock(), session_scope(
            self._session_factory
        ) as session:
            lock_activity_write_plane(session)
            session.execute(
                delete(DecisionRequestReceipt)
                .where(
                    DecisionRequestReceipt.request_id == request_id,
                    DecisionRequestReceipt.expires_at <= current,
                )
                .execution_options(synchronize_session=False)
            )
            self._insert_pending_if_absent(
                session,
                request_id=request_id,
                fingerprint=fingerprint,
                owner_token=owner_token,
                now=current,
                requested_at=initial_requested_at,
            )
            receipt = self._locked_receipt(session, request_id)
            if receipt is None:  # pragma: no cover - insert/select invariant
                raise RuntimeError("decision receipt disappeared after claim")
            _require_fingerprint(
                receipt,
                fingerprint,
                legacy_fingerprint=legacy_fingerprint,
            )
            _normalize_completed_receipt(
                session,
                receipt,
                now=current,
            )

            if receipt.state == DecisionReceiptClaimState.COMPLETED:
                return _completed_claim(receipt)
            if receipt.state == "tombstone":
                expired = True
            elif receipt.state != "pending":
                raise RuntimeError(
                    f"unsupported decision receipt state: {receipt.state}"
                )
            else:
                lease_expires_at = receipt.lease_expires_at
                if lease_expires_at is None:  # pragma: no cover - DB constraint
                    raise RuntimeError("pending decision receipt has no lease")
                lease_expired = _as_utc(lease_expires_at) <= current
                if receipt.owner_token == owner_token:
                    receipt.lease_expires_at = (
                        current + self._lease_duration
                    )
                    session.flush()
                    return DecisionReceiptClaim(
                        state=DecisionReceiptClaimState.ACQUIRED,
                        requested_at=_as_utc(receipt.requested_at),
                        lease_generation=receipt.lease_generation,
                    )
                if lease_expired:
                    receipt.owner_token = owner_token
                    receipt.lease_generation += 1
                    receipt.lease_expires_at = (
                        current + self._lease_duration
                    )
                    session.flush()
                    return DecisionReceiptClaim(
                        state=DecisionReceiptClaimState.ACQUIRED,
                        requested_at=_as_utc(receipt.requested_at),
                        lease_generation=receipt.lease_generation,
                    )

                remaining = max(
                    0.01,
                    (_as_utc(lease_expires_at) - current).total_seconds(),
                )
                return DecisionReceiptClaim(
                    state=DecisionReceiptClaimState.WAIT,
                    requested_at=_as_utc(receipt.requested_at),
                    retry_after_seconds=min(0.1, remaining),
                )
        if expired:
            raise DecisionReceiptExpiredError(
                "idempotency result expired; submit a new idempotency key"
            )
        raise RuntimeError("decision receipt claim did not resolve")

    def complete(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        owner_token: uuid.UUID,
        lease_generation: int,
        result_payload: dict[str, Any],
        now: datetime,
        legacy_fingerprint: str | None = None,
    ) -> DecisionReceiptCompletion:
        current = _as_utc(now)
        expired = False
        # Retention updates use this same process/cross-process lock order.
        # Completion therefore either observes the new policy or commits first
        # and is immediately scrubbed by the waiting retention transaction.
        with activity_write_lock():
            with session_scope(self._session_factory) as session:
                lock_activity_write_plane(session)
                receipt = self._locked_receipt(session, request_id)
                if receipt is None:
                    raise DecisionReceiptOwnershipError(
                        "decision receipt disappeared before completion"
                    )
                _require_fingerprint(
                    receipt,
                    fingerprint,
                    legacy_fingerprint=legacy_fingerprint,
                )
                _normalize_completed_receipt(
                    session,
                    receipt,
                    now=current,
                )
                if receipt.state == DecisionReceiptClaimState.COMPLETED:
                    claim = _completed_claim(receipt)
                    assert claim.result_payload is not None
                    return DecisionReceiptCompletion(
                        result_payload=claim.result_payload,
                        expires_at=claim.expires_at,
                    )
                if receipt.state == "tombstone":
                    expired = True
                elif (
                    receipt.state != "pending"
                    or receipt.owner_token != owner_token
                    or receipt.lease_generation != lease_generation
                    or receipt.lease_expires_at is None
                    or _as_utc(receipt.lease_expires_at) <= current
                ):
                    raise DecisionReceiptOwnershipError(
                        "decision receipt lease is owned by another generation"
                    )
                else:
                    result_expires_at = _result_expiry(
                        session,
                        receipt=receipt,
                        result_payload=result_payload,
                    )
                    receipt.owner_token = None
                    receipt.lease_expires_at = None
                    if result_expires_at <= current:
                        receipt.state = "tombstone"
                        receipt.result_payload = null()
                        receipt.result_expires_at = None
                        session.flush()
                        return DecisionReceiptCompletion(
                            result_payload=dict(result_payload),
                            expires_at=None,
                        )

                    receipt.state = DecisionReceiptClaimState.COMPLETED
                    receipt.result_payload = result_payload
                    receipt.result_expires_at = result_expires_at
                    session.flush()
                    return DecisionReceiptCompletion(
                        result_payload=dict(result_payload),
                        expires_at=result_expires_at,
                    )
        if expired:
            raise DecisionReceiptExpiredError(
                "idempotency result expired; submit a new idempotency key"
            )
        raise RuntimeError("decision receipt completion did not resolve")

    def release(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        owner_token: uuid.UUID,
        lease_generation: int,
        now: datetime,
        legacy_fingerprint: str | None = None,
    ) -> None:
        current = _as_utc(now)
        with activity_write_lock(), session_scope(
            self._session_factory
        ) as session:
            lock_activity_write_plane(session)
            receipt = self._locked_receipt(session, request_id)
            if receipt is None:
                return
            _require_fingerprint(
                receipt,
                fingerprint,
                legacy_fingerprint=legacy_fingerprint,
            )
            if (
                receipt.state != "pending"
                or receipt.owner_token != owner_token
                or receipt.lease_generation != lease_generation
            ):
                return
            # Revoke this exact generation before making the receipt
            # reclaimable. A blocked completion from the cancelled worker can
            # no longer publish after this transaction commits.
            receipt.lease_generation += 1
            receipt.lease_expires_at = current
            session.flush()

    def observe(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        now: datetime,
        legacy_fingerprint: str | None = None,
    ) -> DecisionReceiptClaim:
        """Read canonical state without taking over a live lease."""

        current = _as_utc(now)
        expired = False
        with activity_write_lock(), session_scope(
            self._session_factory
        ) as session:
            lock_activity_write_plane(session)
            receipt = self._locked_receipt(session, request_id)
            if receipt is None:
                raise DecisionReceiptOwnershipError(
                    "decision receipt disappeared while awaiting canonical result"
                )
            _require_fingerprint(
                receipt,
                fingerprint,
                legacy_fingerprint=legacy_fingerprint,
            )
            _normalize_completed_receipt(
                session,
                receipt,
                now=current,
            )
            if receipt.state == DecisionReceiptClaimState.COMPLETED:
                return _completed_claim(receipt)
            if receipt.state == "tombstone":
                expired = True
            elif receipt.state != "pending":
                raise RuntimeError(
                    f"unsupported decision receipt state: {receipt.state}"
                )
            else:
                lease_expires_at = receipt.lease_expires_at
                if lease_expires_at is None:  # pragma: no cover - DB constraint
                    raise RuntimeError("pending decision receipt has no lease")
                remaining_seconds = (
                    _as_utc(lease_expires_at) - current
                ).total_seconds()
                lease_expired = remaining_seconds <= 0
                return DecisionReceiptClaim(
                    state=DecisionReceiptClaimState.WAIT,
                    requested_at=_as_utc(receipt.requested_at),
                    lease_generation=receipt.lease_generation,
                    lease_expired=lease_expired,
                    retry_after_seconds=(
                        0.0
                        if lease_expired
                        else min(0.1, max(0.01, remaining_seconds))
                    ),
                )
        if expired:
            raise DecisionReceiptExpiredError(
                "idempotency result expired; submit a new idempotency key"
            )
        raise RuntimeError("decision receipt observation did not resolve")

    def _insert_pending_if_absent(
        self,
        session: Session,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        owner_token: uuid.UUID,
        now: datetime,
        requested_at: datetime,
    ) -> None:
        values = {
            "id": uuid.uuid4(),
            "request_id": request_id,
            "request_fingerprint": fingerprint,
            "requested_at": requested_at,
            "state": "pending",
            "owner_token": owner_token,
            "lease_generation": 1,
            "lease_expires_at": now + self._lease_duration,
            # SQLAlchemy JSON persists Python None as JSON "null" by
            # default. Pending-state constraints require SQL NULL.
            "result_payload": null(),
            "result_expires_at": None,
            # Unlike requested_at, this is a trusted server receive time.
            # It is immutable after the winning insert and therefore remains
            # stable across waiters, lease takeover, and replay.
            "retention_basis_at": now,
            "expires_at": now + self._retention,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = pg_insert(DecisionRequestReceipt).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(DecisionRequestReceipt).values(
                **values
            )
        else:
            raise RuntimeError(
                "decision receipts support only sqlite and postgresql"
            )
        session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[DecisionRequestReceipt.request_id]
            )
        )

    @staticmethod
    def _locked_receipt(
        session: Session,
        request_id: uuid.UUID,
    ) -> DecisionRequestReceipt | None:
        statement = select(DecisionRequestReceipt).where(
            DecisionRequestReceipt.request_id == request_id
        )
        if session.get_bind().dialect.name == "postgresql":
            lock_activity_write_plane(session)
            statement = statement.with_for_update()
        return session.scalar(statement)


def scrub_decision_receipt_results(
    session: Session,
    *,
    now: datetime,
    dry_run: bool = False,
    batch_size: int = DEFAULT_RECEIPT_MAINTENANCE_BATCH_SIZE,
) -> int:
    """Apply current retention to every completed receipt."""

    current = _as_utc(now)
    if batch_size < 1:
        raise ValueError("decision receipt batch_size must be positive")

    if session.get_bind().dialect.name == "postgresql" and not dry_run:
        lock_activity_write_plane(session)

    candidates = 0
    last_id: uuid.UUID | None = None
    while True:
        statement = (
            select(DecisionRequestReceipt)
            .where(DecisionRequestReceipt.state == "completed")
            .order_by(DecisionRequestReceipt.id)
            .limit(batch_size)
        )
        if last_id is not None:
            statement = statement.where(
                DecisionRequestReceipt.id > last_id
            )
        if (
            session.get_bind().dialect.name == "postgresql"
            and not dry_run
        ):
            statement = statement.with_for_update()
        rows = tuple(session.scalars(statement))
        if not rows:
            break
        for receipt in rows:
            try:
                expired = _normalize_completed_receipt(
                    session,
                    receipt,
                    now=current,
                )
            except RuntimeError:
                expired = True
                if not dry_run:
                    _tombstone_receipt_result(receipt)
            if expired:
                candidates += 1
            if dry_run:
                session.expire(receipt)
        last_id = rows[-1].id
        if not dry_run:
            session.flush()
        if len(rows) < batch_size:
            break

    if not dry_run:
        session.flush()
    return candidates


def maintain_decision_receipt_results(
    session: Session,
    *,
    now: datetime,
    batch_size: int = DEFAULT_RECEIPT_MAINTENANCE_BATCH_SIZE,
    after_id: uuid.UUID | None = None,
) -> DecisionReceiptMaintenanceBatch:
    """Scan and normalize one bounded page of completed receipts."""

    current = _as_utc(now)
    if batch_size < 1:
        raise ValueError("decision receipt batch_size must be positive")
    statement = (
        select(DecisionRequestReceipt)
        .where(DecisionRequestReceipt.state == "completed")
        .order_by(DecisionRequestReceipt.id)
        .limit(batch_size)
    )
    if after_id is not None:
        statement = statement.where(
            DecisionRequestReceipt.id > after_id
        )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    for receipt in rows:
        try:
            _normalize_completed_receipt(
                session,
                receipt,
                now=current,
            )
        except RuntimeError:
            # A corrupt completed envelope must not poison every startup and
            # recurring maintenance transaction. Its cached body is already
            # unusable, so fail closed and continue cleaning the batch.
            _tombstone_receipt_result(receipt)
    session.flush()
    return DecisionReceiptMaintenanceBatch(
        scanned=len(rows),
        next_cursor=(
            rows[-1].id
            if len(rows) == batch_size
            else None
        ),
    )


def purge_expired_decision_receipts(
    session: Session,
    *,
    now: datetime,
    dry_run: bool = False,
) -> int:
    """Delete identities after first scrubbing any expired result payload."""

    current = _as_utc(now)
    scrub_decision_receipt_results(
        session,
        now=current,
        dry_run=dry_run,
    )
    condition = DecisionRequestReceipt.expires_at <= current
    candidates = int(
        session.scalar(
            select(func.count())
            .select_from(DecisionRequestReceipt)
            .where(condition)
        )
        or 0
    )
    if candidates and not dry_run:
        session.execute(
            delete(DecisionRequestReceipt)
            .where(condition)
            .execution_options(synchronize_session=False)
        )
    return candidates


def _completed_claim(
    receipt: DecisionRequestReceipt,
) -> DecisionReceiptClaim:
    if not isinstance(receipt.result_payload, dict):
        raise RuntimeError("completed decision receipt has no result payload")
    if receipt.result_expires_at is None:
        raise RuntimeError(
            "completed decision receipt has no result expiry"
        )
    return DecisionReceiptClaim(
        state=DecisionReceiptClaimState.COMPLETED,
        result_payload=dict(receipt.result_payload),
        expires_at=_as_utc(receipt.result_expires_at),
        requested_at=_as_utc(receipt.requested_at),
        lease_generation=receipt.lease_generation,
    )


def _normalize_completed_receipt(
    session: Session,
    receipt: DecisionRequestReceipt,
    *,
    now: datetime,
) -> bool:
    if receipt.state != "completed":
        return False
    payload, deadline = _completed_receipt_normalization(
        session,
        receipt,
    )
    if deadline <= _as_utc(now):
        _tombstone_receipt_result(receipt)
        return True
    if payload != receipt.result_payload:
        receipt.result_payload = payload
    receipt.result_expires_at = deadline
    return False


def _tombstone_receipt_result(
    receipt: DecisionRequestReceipt,
) -> None:
    receipt.state = "tombstone"
    receipt.owner_token = None
    receipt.lease_expires_at = None
    receipt.result_payload = null()
    receipt.result_expires_at = None


def _completed_receipt_normalization(
    session: Session,
    receipt: DecisionRequestReceipt,
) -> tuple[dict[str, Any], datetime]:
    if not isinstance(receipt.result_payload, dict):
        raise RuntimeError("completed decision receipt has no result payload")
    if receipt.result_expires_at is None:
        raise RuntimeError(
            "completed decision receipt has no result expiry"
        )
    payload = _compact_receipt_payload(receipt.result_payload)
    # Sensitive replay payloads have monotonic expiry. Extending a policy may
    # retain future results longer, but it cannot revive a committed deadline.
    deadline = min(
        _as_utc(receipt.result_expires_at),
        _result_expiry(
            session,
            receipt=receipt,
            result_payload=payload,
        ),
    )
    return payload, deadline


def _result_expiry(
    session: Session,
    *,
    receipt: DecisionRequestReceipt,
    result_payload: dict[str, Any] | None = None,
) -> datetime:
    identity_deadline = _as_utc(receipt.expires_at)
    if _receipt_payload_is_transient(result_payload or receipt.result_payload):
        identity_deadline = min(
            identity_deadline,
            _as_utc(receipt.retention_basis_at)
            + _TRANSIENT_RESULT_MAX_AGE,
        )
    policy = session.scalar(
        select(RetentionPolicy)
        .where(RetentionPolicy.data_class == "decision")
        .limit(1)
    )
    if (
        policy is None
        or not policy.enabled
        or policy.retention_days is None
    ):
        return identity_deadline
    decision_deadline = _as_utc(
        receipt.retention_basis_at
    ) + timedelta(days=policy.retention_days)
    return min(identity_deadline, decision_deadline)


def _compact_receipt_payload(
    payload: Any,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("completed decision receipt has no result payload")
    normalized = dict(payload)
    schema_name = normalized.get("schema")
    if schema_name == _DECISION_RECEIPT_SCHEMA_V2:
        kind = normalized.get("kind")
        if kind == "decision_record":
            pointer = _canonical_decision_record_pointer(normalized)
            if pointer is None:
                raise RuntimeError(
                    "persisted decision receipt is invalid"
                )
            return pointer
        if (
            kind == "transient_result"
            and set(normalized) == {"schema", "kind", "result"}
            and isinstance(normalized.get("result"), dict)
        ):
            return normalized
        raise RuntimeError("decision receipt result is invalid")
    if schema_name != _DECISION_RECEIPT_SCHEMA_V1:
        raise RuntimeError("unsupported decision receipt schema")
    if set(normalized) != {"schema", "result"}:
        raise RuntimeError("legacy decision receipt is invalid")
    raw_result = normalized.get("result")
    if not isinstance(raw_result, dict):
        raise RuntimeError("legacy decision receipt result is invalid")
    if (
        raw_result.get("persistence_status") == "persisted"
        and raw_result.get("decision_record_id") is not None
    ):
        try:
            record_id = uuid.UUID(str(raw_result["decision_record_id"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "legacy persisted decision receipt is invalid"
            ) from exc
        return {
            "schema": _DECISION_RECEIPT_SCHEMA_V2,
            "kind": "decision_record",
            "decision_record_id": str(record_id),
        }
    return {
        "schema": _DECISION_RECEIPT_SCHEMA_V2,
        "kind": "transient_result",
        "result": raw_result,
    }


def _canonical_decision_record_pointer(
    payload: Any,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "kind",
        "decision_record_id",
    }:
        return None
    if (
        payload.get("schema") != _DECISION_RECEIPT_SCHEMA_V2
        or payload.get("kind") != "decision_record"
    ):
        return None
    raw_record_id = payload.get("decision_record_id")
    if not isinstance(raw_record_id, str):
        return None
    try:
        record_id = uuid.UUID(raw_record_id)
    except ValueError:
        return None
    canonical_id = str(record_id)
    if raw_record_id != canonical_id:
        return None
    return {
        "schema": _DECISION_RECEIPT_SCHEMA_V2,
        "kind": "decision_record",
        "decision_record_id": canonical_id,
    }


def _receipt_payload_is_transient(payload: Any) -> bool:
    return _canonical_decision_record_pointer(payload) is None


def _require_fingerprint(
    receipt: DecisionRequestReceipt,
    fingerprint: str,
    *,
    legacy_fingerprint: str | None = None,
) -> None:
    stored = receipt.request_fingerprint
    if hmac.compare_digest(stored, fingerprint):
        return
    if (
        legacy_fingerprint is not None
        and hmac.compare_digest(stored, legacy_fingerprint)
    ):
        # Upgrade the retained identity while this receipt is locked so
        # low-entropy legacy digests do not remain readable until expiry.
        receipt.request_fingerprint = fingerprint
        return
    raise DecisionReceiptConflictError(
        "decision request id was reused with different input"
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
