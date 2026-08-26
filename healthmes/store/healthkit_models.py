"""Durable idempotency and deletion state for first-party HealthKit sync."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from healthmes.store.base import Base, JSONDict, str_32, str_64, str_255

__all__ = [
    "HealthKitDeletionTombstone",
    "HealthKitIngestReceipt",
]


class HealthKitIngestReceipt(Base):
    """One stable Idempotency-Key mapped to exact request bytes and its ACK."""

    __tablename__ = "healthkit_ingest_receipt"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key_digest",
            name="uq_healthkit_ingest_receipt_idempotency_key_digest",
        ),
        CheckConstraint(
            "lease_generation >= 1",
            name="lease_generation_positive",
        ),
        CheckConstraint(
            "("
            "state = 'pending' "
            "AND owner_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL "
            "AND ack_payload IS NULL"
            ") OR ("
            "state = 'completed' "
            "AND owner_token IS NULL "
            "AND lease_expires_at IS NULL "
            "AND ack_payload IS NOT NULL"
            ")",
            name="state_consistent",
        ),
    )

    idempotency_key_digest: Mapped[str_64]
    body_sha256: Mapped[str_64]
    state: Mapped[str_32] = mapped_column(default="pending")
    owner_token: Mapped[uuid.UUID | None]
    lease_generation: Mapped[int] = mapped_column(default=1)
    lease_expires_at: Mapped[datetime | None] = mapped_column(index=True)
    raw_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_ingest_event.id", ondelete="SET NULL"),
        index=True,
    )
    ack_payload: Mapped[JSONDict | None]


class HealthKitDeletionTombstone(Base):
    """A durable HealthKit object deletion used to suppress stale replays."""

    __tablename__ = "healthkit_deletion_tombstone"
    __table_args__ = (
        UniqueConstraint(
            "sample_id",
            name="uq_healthkit_deletion_tombstone_sample_id",
        ),
        Index(
            "ix_healthkit_deletion_tombstone_idempotency_key_digest",
            "idempotency_key_digest",
        ),
    )

    sample_id: Mapped[str_255]
    sample_type: Mapped[str_255]
    deleted_at: Mapped[datetime] = mapped_column(index=True)
    raw_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_ingest_event.id", ondelete="SET NULL"),
        index=True,
    )
    idempotency_key_digest: Mapped[str_64 | None]
