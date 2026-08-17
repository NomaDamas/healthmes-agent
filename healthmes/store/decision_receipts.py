"""Atomic, content-minimized idempotency receipts for decision requests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, null, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from healthmes.store.models import DecisionRequestReceipt
from healthmes.store.session import session_scope


class DecisionReceiptConflictError(RuntimeError):
    """The same request identity was reused with different input."""


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
    retry_after_seconds: float = 0.05


@dataclass(frozen=True, slots=True)
class DecisionReceiptCompletion:
    result_payload: dict[str, Any]
    expires_at: datetime


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
    ) -> DecisionReceiptClaim:
        current = _as_utc(now)
        initial_requested_at = _as_utc(requested_at or current)
        with session_scope(self._session_factory) as session:
            session.execute(
                delete(DecisionRequestReceipt)
                .where(
                    DecisionRequestReceipt.expires_at <= current
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
            _require_fingerprint(receipt, fingerprint)

            if receipt.state == DecisionReceiptClaimState.COMPLETED:
                if not isinstance(receipt.result_payload, dict):
                    raise RuntimeError("completed decision receipt has no result payload")
                return DecisionReceiptClaim(
                    state=DecisionReceiptClaimState.COMPLETED,
                    result_payload=dict(receipt.result_payload),
                    expires_at=_as_utc(receipt.expires_at),
                    requested_at=_as_utc(receipt.requested_at),
                )

            if receipt.state != "pending":
                raise RuntimeError(f"unsupported decision receipt state: {receipt.state}")
            lease_expires_at = receipt.lease_expires_at
            if lease_expires_at is None:  # pragma: no cover - DB constraint
                raise RuntimeError("pending decision receipt has no lease")
            if receipt.owner_token == owner_token or _as_utc(lease_expires_at) <= current:
                receipt.owner_token = owner_token
                receipt.lease_expires_at = current + self._lease_duration
                receipt.expires_at = current + self._retention
                session.flush()
                return DecisionReceiptClaim(
                    state=DecisionReceiptClaimState.ACQUIRED,
                    requested_at=_as_utc(receipt.requested_at),
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

    def complete(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        owner_token: uuid.UUID,
        result_payload: dict[str, Any],
        now: datetime,
    ) -> DecisionReceiptCompletion:
        current = _as_utc(now)
        with session_scope(self._session_factory) as session:
            receipt = self._locked_receipt(session, request_id)
            if receipt is None:
                raise DecisionReceiptOwnershipError(
                    "decision receipt disappeared before completion"
                )
            _require_fingerprint(receipt, fingerprint)
            if receipt.state == DecisionReceiptClaimState.COMPLETED:
                if not isinstance(receipt.result_payload, dict):
                    raise RuntimeError("completed decision receipt has no result payload")
                return DecisionReceiptCompletion(
                    result_payload=dict(receipt.result_payload),
                    expires_at=_as_utc(receipt.expires_at),
                )
            if receipt.state != "pending" or receipt.owner_token != owner_token:
                raise DecisionReceiptOwnershipError(
                    "decision receipt lease is owned by another worker"
                )
            receipt.state = DecisionReceiptClaimState.COMPLETED
            receipt.owner_token = None
            receipt.lease_expires_at = None
            receipt.result_payload = result_payload
            receipt.expires_at = current + self._retention
            session.flush()
            return DecisionReceiptCompletion(
                result_payload=dict(result_payload),
                expires_at=_as_utc(receipt.expires_at),
            )

    def release(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        owner_token: uuid.UUID,
        now: datetime,
    ) -> None:
        current = _as_utc(now)
        with session_scope(self._session_factory) as session:
            receipt = self._locked_receipt(session, request_id)
            if receipt is None:
                return
            _require_fingerprint(receipt, fingerprint)
            if (
                receipt.state != "pending"
                or receipt.owner_token != owner_token
            ):
                return
            # Preserve requested_at and the request identity across transient
            # failures while making the lease immediately reclaimable.
            receipt.lease_expires_at = current
            receipt.expires_at = current + self._retention
            session.flush()

    def observe(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        now: datetime,
    ) -> DecisionReceiptClaim:
        """Read canonical state without taking over a live lease."""

        current = _as_utc(now)
        with session_scope(self._session_factory) as session:
            receipt = self._locked_receipt(session, request_id)
            if receipt is None:
                raise DecisionReceiptOwnershipError(
                    "decision receipt disappeared while awaiting canonical result"
                )
            _require_fingerprint(receipt, fingerprint)
            if receipt.state == DecisionReceiptClaimState.COMPLETED:
                if not isinstance(receipt.result_payload, dict):
                    raise RuntimeError(
                        "completed decision receipt has no result payload"
                    )
                return DecisionReceiptClaim(
                    state=DecisionReceiptClaimState.COMPLETED,
                    result_payload=dict(receipt.result_payload),
                    expires_at=_as_utc(receipt.expires_at),
                    requested_at=_as_utc(receipt.requested_at),
                )
            if receipt.state != "pending":
                raise RuntimeError(
                    f"unsupported decision receipt state: {receipt.state}"
                )
            lease_expires_at = receipt.lease_expires_at
            if lease_expires_at is None:  # pragma: no cover - DB constraint
                raise RuntimeError("pending decision receipt has no lease")
            remaining = max(
                0.01,
                (_as_utc(lease_expires_at) - current).total_seconds(),
            )
            return DecisionReceiptClaim(
                state=DecisionReceiptClaimState.WAIT,
                requested_at=_as_utc(receipt.requested_at),
                retry_after_seconds=min(0.1, remaining),
            )

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
            "lease_expires_at": now + self._lease_duration,
            # SQLAlchemy JSON persists Python None as JSON "null" by
            # default. The pending-state constraint requires SQL NULL.
            "result_payload": null(),
            "expires_at": now + self._retention,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = pg_insert(DecisionRequestReceipt).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(DecisionRequestReceipt).values(**values)
        else:
            raise RuntimeError("decision receipts support only sqlite and postgresql")
        session.execute(
            statement.on_conflict_do_nothing(index_elements=[DecisionRequestReceipt.request_id])
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
            statement = statement.with_for_update()
        return session.scalar(statement)


def purge_expired_decision_receipts(
    session: Session,
    *,
    now: datetime,
    dry_run: bool = False,
) -> int:
    """Delete receipts at or beyond their bounded retention cutoff."""

    current = _as_utc(now)
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


def _require_fingerprint(
    receipt: DecisionRequestReceipt,
    fingerprint: str,
) -> None:
    if receipt.request_fingerprint != fingerprint:
        raise DecisionReceiptConflictError("decision request id was reused with different input")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
