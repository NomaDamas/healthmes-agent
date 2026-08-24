"""Transactional HealthKit receipt and deletion-tombstone persistence."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import null, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from healthmes.activity.locking import global_write_plane_guard
from healthmes.store.healthkit_models import (
    HealthKitDeletionTombstone,
    HealthKitIngestReceipt,
)

DEFAULT_HEALTHKIT_RECEIPT_LEASE = timedelta(minutes=5)


class HealthKitIdempotencyConflictError(RuntimeError):
    """The same key was reused with different exact request bytes."""


class HealthKitReceiptOwnershipError(RuntimeError):
    """A request no longer owns the receipt generation it is completing."""


class HealthKitReceiptClaimState(StrEnum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class HealthKitReceiptClaim:
    state: HealthKitReceiptClaimState
    key_digest: str
    body_sha256: str
    lease_generation: int | None = None
    raw_id: uuid.UUID | None = None
    ack_payload: dict[str, Any] | None = None
    retry_after_seconds: float = 0.05


def healthkit_idempotency_key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class HealthKitIngestStore:
    """Coordinates exact-byte replay and durable HealthKit tombstones."""

    def __init__(
        self,
        bind,
        *,
        lease_duration: timedelta = DEFAULT_HEALTHKIT_RECEIPT_LEASE,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._bind = bind
        self._lease_duration = lease_duration

    def claim(
        self,
        *,
        idempotency_key: str,
        body_sha256: str,
        owner_token: uuid.UUID,
        now: datetime,
    ) -> HealthKitReceiptClaim:
        current = _as_utc(now)
        key_digest = healthkit_idempotency_key_digest(idempotency_key)
        with global_write_plane_guard(self._bind) as guard_connection:
            writer_bind = (
                guard_connection if guard_connection is not None else self._bind
            )
            with Session(bind=writer_bind) as session:
                self._insert_pending_if_absent(
                    session,
                    key_digest=key_digest,
                    body_sha256=body_sha256,
                    owner_token=owner_token,
                    now=current,
                    lease_duration=self._lease_duration,
                )
                receipt = self._locked_receipt(session, key_digest)
                if receipt is None:  # pragma: no cover - insert/select invariant
                    raise RuntimeError("HealthKit receipt disappeared after claim")
                self._require_body(receipt, body_sha256)

                if receipt.state == HealthKitReceiptClaimState.COMPLETED:
                    if not isinstance(receipt.ack_payload, dict):
                        raise RuntimeError(
                            "completed HealthKit receipt has no ACK payload"
                        )
                    result = HealthKitReceiptClaim(
                        state=HealthKitReceiptClaimState.COMPLETED,
                        key_digest=key_digest,
                        body_sha256=body_sha256,
                        raw_id=receipt.raw_id,
                        ack_payload=dict(receipt.ack_payload),
                    )
                elif receipt.state != "pending":
                    raise RuntimeError(
                        f"unsupported HealthKit receipt state: {receipt.state}"
                    )
                else:
                    lease_expires_at = receipt.lease_expires_at
                    if lease_expires_at is None:  # pragma: no cover - DB constraint
                        raise RuntimeError("pending HealthKit receipt has no lease")
                    lease_expired = _as_utc(lease_expires_at) <= current
                    if receipt.owner_token == owner_token or lease_expired:
                        if receipt.owner_token != owner_token:
                            receipt.owner_token = owner_token
                            receipt.lease_generation += 1
                        receipt.lease_expires_at = current + self._lease_duration
                        session.flush()
                        result = HealthKitReceiptClaim(
                            state=HealthKitReceiptClaimState.ACQUIRED,
                            key_digest=key_digest,
                            body_sha256=body_sha256,
                            lease_generation=receipt.lease_generation,
                            raw_id=receipt.raw_id,
                        )
                    else:
                        remaining = max(
                            0.01,
                            (_as_utc(lease_expires_at) - current).total_seconds(),
                        )
                        result = HealthKitReceiptClaim(
                            state=HealthKitReceiptClaimState.WAIT,
                            key_digest=key_digest,
                            body_sha256=body_sha256,
                            raw_id=receipt.raw_id,
                            retry_after_seconds=min(0.1, remaining),
                        )
                session.commit()
                return result

    @classmethod
    def attach_raw_in_session(
        cls,
        session: Session,
        *,
        claim: HealthKitReceiptClaim,
        owner_token: uuid.UUID,
        raw_id: uuid.UUID,
        now: datetime,
        lease_duration: timedelta = DEFAULT_HEALTHKIT_RECEIPT_LEASE,
    ) -> None:
        receipt = cls._locked_receipt(session, claim.key_digest)
        cls._require_owned(
            receipt,
            claim=claim,
            owner_token=owner_token,
        )
        assert receipt is not None
        cls._require_body(receipt, claim.body_sha256)
        if receipt.raw_id is None:
            receipt.raw_id = raw_id
        elif receipt.raw_id != raw_id:
            raise HealthKitReceiptOwnershipError(
                "HealthKit receipt is attached to another raw payload"
            )
        receipt.lease_expires_at = _as_utc(now) + lease_duration

    def apply_deletions(
        self,
        *,
        raw_id: uuid.UUID,
        deletions: tuple[tuple[str, str], ...],
        candidate_ids: set[str],
        now: datetime,
        claim: HealthKitReceiptClaim | None = None,
        owner_token: uuid.UUID | None = None,
    ) -> set[str]:
        current = _as_utc(now)
        with global_write_plane_guard(self._bind) as guard_connection:
            writer_bind = (
                guard_connection if guard_connection is not None else self._bind
            )
            with Session(bind=writer_bind) as session:
                if claim is not None:
                    if owner_token is None:
                        raise ValueError("owner_token is required with a receipt")
                    receipt = self._locked_receipt(session, claim.key_digest)
                    self._require_owned(
                        receipt,
                        claim=claim,
                        owner_token=owner_token,
                    )
                    assert receipt is not None
                    receipt.lease_expires_at = current + self._lease_duration

                for sample_id, sample_type in deletions:
                    self._upsert_tombstone(
                        session,
                        sample_id=sample_id,
                        sample_type=sample_type,
                        deleted_at=current,
                        raw_id=raw_id,
                        key_digest=claim.key_digest if claim is not None else None,
                    )
                suppressed = self._tombstoned_ids(session, candidate_ids)
                try:
                    session.commit()
                except BaseException:
                    session.rollback()
                    if not self._deletions_are_durable(
                        writer_bind,
                        deletions=deletions,
                    ):
                        raise
                    suppressed = self._tombstoned_ids_from_bind(
                        writer_bind,
                        candidate_ids,
                    )
                return suppressed

    def complete(
        self,
        *,
        claim: HealthKitReceiptClaim,
        owner_token: uuid.UUID,
        ack_payload: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        stored: dict[str, Any] | None = None

        def mutation(
            receipt: HealthKitIngestReceipt,
            current: datetime,
        ) -> None:
            nonlocal stored
            if receipt.state == HealthKitReceiptClaimState.COMPLETED:
                if not isinstance(receipt.ack_payload, dict):
                    raise RuntimeError(
                        "completed HealthKit receipt has no ACK payload"
                    )
                stored = dict(receipt.ack_payload)
                return
            receipt.state = HealthKitReceiptClaimState.COMPLETED
            receipt.owner_token = None
            receipt.lease_expires_at = None
            receipt.ack_payload = dict(ack_payload)
            stored = dict(ack_payload)

        self._mutate_owned_receipt(
            claim=claim,
            owner_token=owner_token,
            now=now,
            operation=mutation,
            verifier=lambda receipt: (
                receipt.state == HealthKitReceiptClaimState.COMPLETED
                and receipt.ack_payload == ack_payload
            ),
            allow_completed=True,
        )
        assert stored is not None
        return stored

    def release(
        self,
        *,
        claim: HealthKitReceiptClaim,
        owner_token: uuid.UUID,
        now: datetime,
    ) -> None:
        current = _as_utc(now)
        with global_write_plane_guard(self._bind) as guard_connection:
            writer_bind = (
                guard_connection if guard_connection is not None else self._bind
            )
            with Session(bind=writer_bind) as session:
                receipt = self._locked_receipt(session, claim.key_digest)
                if receipt is None or receipt.state != "pending":
                    return
                if (
                    receipt.owner_token != owner_token
                    or receipt.lease_generation != claim.lease_generation
                ):
                    return
                receipt.lease_generation += 1
                receipt.lease_expires_at = current
                session.commit()

    def _mutate_owned_receipt(
        self,
        *,
        claim: HealthKitReceiptClaim,
        owner_token: uuid.UUID,
        now: datetime,
        operation,
        verifier,
        allow_completed: bool = False,
    ) -> None:
        current = _as_utc(now)
        with global_write_plane_guard(self._bind) as guard_connection:
            writer_bind = (
                guard_connection if guard_connection is not None else self._bind
            )
            with Session(bind=writer_bind) as session:
                receipt = self._locked_receipt(session, claim.key_digest)
                if not (
                    allow_completed
                    and receipt is not None
                    and receipt.state == HealthKitReceiptClaimState.COMPLETED
                ):
                    self._require_owned(
                        receipt,
                        claim=claim,
                        owner_token=owner_token,
                    )
                assert receipt is not None
                self._require_body(receipt, claim.body_sha256)
                operation(receipt, current)
                if receipt.state == "pending":
                    receipt.lease_expires_at = current + self._lease_duration
                try:
                    session.commit()
                except BaseException:
                    session.rollback()
                    verified = self._receipt_from_bind(
                        writer_bind,
                        claim.key_digest,
                    )
                    if verified is not None and verifier(verified):
                        return
                    raise

    @staticmethod
    def _require_body(
        receipt: HealthKitIngestReceipt,
        body_sha256: str,
    ) -> None:
        if not hmac.compare_digest(receipt.body_sha256, body_sha256):
            raise HealthKitIdempotencyConflictError(
                "Idempotency-Key was already used for different exact bytes"
            )

    @staticmethod
    def _require_owned(
        receipt: HealthKitIngestReceipt | None,
        *,
        claim: HealthKitReceiptClaim,
        owner_token: uuid.UUID,
    ) -> None:
        if (
            receipt is None
            or receipt.state != "pending"
            or receipt.owner_token != owner_token
            or receipt.lease_generation != claim.lease_generation
        ):
            raise HealthKitReceiptOwnershipError(
                "HealthKit receipt lease is owned by another generation"
            )

    @staticmethod
    def _locked_receipt(
        session: Session,
        key_digest: str,
    ) -> HealthKitIngestReceipt | None:
        statement = select(HealthKitIngestReceipt).where(
            HealthKitIngestReceipt.idempotency_key_digest == key_digest
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return session.scalar(statement)

    @staticmethod
    def _receipt_from_bind(
        bind,
        key_digest: str,
    ) -> HealthKitIngestReceipt | None:
        with Session(bind=bind) as session:
            return session.scalar(
                select(HealthKitIngestReceipt).where(
                    HealthKitIngestReceipt.idempotency_key_digest == key_digest
                )
            )

    @staticmethod
    def _insert_pending_if_absent(
        session: Session,
        *,
        key_digest: str,
        body_sha256: str,
        owner_token: uuid.UUID,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        values = {
            "id": uuid.uuid4(),
            "idempotency_key_digest": key_digest,
            "body_sha256": body_sha256,
            "state": "pending",
            "owner_token": owner_token,
            "lease_generation": 1,
            "lease_expires_at": now + lease_duration,
            "raw_id": None,
            "ack_payload": null(),
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = pg_insert(HealthKitIngestReceipt).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(HealthKitIngestReceipt).values(**values)
        else:
            raise RuntimeError(
                "HealthKit receipts support only sqlite and postgresql"
            )
        session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[
                    HealthKitIngestReceipt.idempotency_key_digest
                ]
            )
        )

    @staticmethod
    def _upsert_tombstone(
        session: Session,
        *,
        sample_id: str,
        sample_type: str,
        deleted_at: datetime,
        raw_id: uuid.UUID,
        key_digest: str | None,
    ) -> None:
        values = {
            "id": uuid.uuid4(),
            "sample_id": sample_id,
            "sample_type": sample_type,
            "deleted_at": deleted_at,
            "raw_id": raw_id,
            "idempotency_key_digest": key_digest,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = pg_insert(HealthKitDeletionTombstone).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(HealthKitDeletionTombstone).values(
                **values
            )
        else:
            raise RuntimeError(
                "HealthKit tombstones support only sqlite and postgresql"
            )
        excluded = statement.excluded
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[HealthKitDeletionTombstone.sample_id],
                set_={
                    "sample_type": excluded.sample_type,
                    "deleted_at": excluded.deleted_at,
                    "raw_id": excluded.raw_id,
                    "idempotency_key_digest": (
                        excluded.idempotency_key_digest
                    ),
                    "updated_at": deleted_at,
                },
            )
        )

    @staticmethod
    def _tombstoned_ids(
        session: Session,
        candidate_ids: set[str],
    ) -> set[str]:
        if not candidate_ids:
            return set()
        return set(
            session.scalars(
                select(HealthKitDeletionTombstone.sample_id).where(
                    HealthKitDeletionTombstone.sample_id.in_(candidate_ids)
                )
            )
        )

    @classmethod
    def _tombstoned_ids_from_bind(
        cls,
        bind,
        candidate_ids: set[str],
    ) -> set[str]:
        with Session(bind=bind) as session:
            return cls._tombstoned_ids(session, candidate_ids)

    @staticmethod
    def _deletions_are_durable(
        bind,
        *,
        deletions: tuple[tuple[str, str], ...],
    ) -> bool:
        if not deletions:
            return True
        expected = {
            sample_id: sample_type
            for sample_id, sample_type in deletions
        }
        with Session(bind=bind) as session:
            rows = session.execute(
                select(
                    HealthKitDeletionTombstone.sample_id,
                    HealthKitDeletionTombstone.sample_type,
                ).where(
                    HealthKitDeletionTombstone.sample_id.in_(expected)
                )
            )
            actual = {sample_id: sample_type for sample_id, sample_type in rows}
        return all(
            actual.get(sample_id) == sample_type
            for sample_id, sample_type in expected.items()
        )
