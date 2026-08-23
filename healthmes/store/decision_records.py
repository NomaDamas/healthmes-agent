"""Retention-aware read helpers for persisted decision records."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, or_

from healthmes.store.models import DecisionRecord

__all__ = [
    "decision_record_is_available",
    "decision_record_is_available_at",
]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def decision_record_is_available_at(
    at: datetime,
) -> ColumnElement[bool]:
    """Return the SQL boundary for records visible at ``at``.

    Expiry is exclusive: a record with ``expires_at == at`` is no longer
    available.
    """

    current = _as_utc(at)
    return or_(
        DecisionRecord.expires_at.is_(None),
        DecisionRecord.expires_at > current,
    )


def decision_record_is_available(
    record: DecisionRecord,
    *,
    at: datetime,
) -> bool:
    """Apply the same exclusive expiry boundary to an already-loaded row."""

    return (
        record.expires_at is None
        or _as_utc(record.expires_at) > _as_utc(at)
    )
