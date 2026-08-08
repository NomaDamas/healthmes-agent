"""backfill legacy food logs into the nutrition intake ledger

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-08 12:00:00
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NAMESPACE = uuid.UUID("7353b772-9d8d-5ba2-b75e-3f38ab09cddd")
ARCHIVE_PROVIDER = "legacy-food-log-archive"
MIGRATION_SCHEMA = "legacy-food-log-archive-v1"
RETENTION_DEFAULTS: dict[str, int | None] = {
    "legacy_food_log_archive": None,
    "nutrition_media": 7,
    "nutrition_raw_capture": 14,
    "nutrition_observation": 90,
    "nutrition_confirmation": None,
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


def _optional_iso(value: datetime | None) -> str | None:
    return _iso(value) if value is not None else None


def _database_uuid(value: str, dialect_name: str) -> uuid.UUID | str:
    parsed = uuid.UUID(value)
    return parsed.hex if dialect_name == "sqlite" else parsed


def _uuid(kind: str, legacy_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{kind}:{legacy_id}")


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "migration_schema": MIGRATION_SCHEMA,
        "id": str(row["id"]),
        "logged_at": _iso(row["logged_at"]),
        "description": row["description"],
        "media_path": row["media_path"],
        "meal_type": row["meal_type"],
        "source": row["source"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _checksum(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _fingerprint(kind: str, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"{kind}:{_canonical(payload)}".encode()).hexdigest()


def _unknown_serving() -> dict[str, Any]:
    return {
        "kind": "unknown",
        "unit": "serving",
        "exact": None,
        "minimum": None,
        "maximum": None,
        "evidence_text": None,
        "estimation_basis": "legacy_food_log",
    }


def _event_table() -> sa.Table:
    return sa.table(
        "wellness_event",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("event_type", sa.String(64)),
        sa.column("schema_version", sa.Integer()),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("recorded_at", sa.DateTime(timezone=True)),
        sa.column("timezone", sa.String(64)),
        sa.column("source_provider", sa.String(64)),
        sa.column("source_device", sa.String(255)),
        sa.column("source_record_id", sa.String(255)),
        sa.column("capture_method", sa.String(32)),
        sa.column("quality_flags", JSONB),
        sa.column("confidence", sa.Float()),
        sa.column("coverage", sa.Float()),
        sa.column("sensitivity", sa.String(32)),
        sa.column("consent_scope", sa.String(64)),
        sa.column("retention_policy_id", sa.Uuid()),
        sa.column("expires_at", sa.DateTime(timezone=True)),
        sa.column("payload", JSONB),
        sa.column("raw_object_id", sa.Uuid()),
        sa.column("derived_from", JSONB),
    )


def _retention_policies(
    bind: sa.Connection,
    retention_policy: sa.Table,
) -> dict[str, Mapping[str, Any]]:
    policies = {
        row["data_class"]: row
        for row in bind.execute(sa.select(retention_policy)).mappings()
    }
    now = datetime.now(UTC)
    missing = []
    for data_class, retention_days in RETENTION_DEFAULTS.items():
        if data_class in policies:
            continue
        policy_id = _uuid("retention-policy", uuid.uuid5(NAMESPACE, data_class))
        missing.append(
            {
                "id": _database_uuid(str(policy_id), bind.dialect.name),
                "created_at": now,
                "updated_at": now,
                "data_class": data_class,
                "retention_days": retention_days,
                "enabled": True,
            }
        )
    if missing:
        bind.execute(sa.insert(retention_policy), missing)
        policies = {
            row["data_class"]: row
            for row in bind.execute(sa.select(retention_policy)).mappings()
        }
    return policies


def _expiry(
    policy: Mapping[str, Any],
    observed_at: datetime,
) -> datetime | None:
    retention_days = policy["retention_days"]
    if not policy["enabled"] or retention_days is None:
        return None
    return _aware(observed_at) + timedelta(days=int(retention_days))


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute("-- FoodLog backfill requires an online Alembic upgrade.")
        return
    bind = op.get_bind()
    metadata = sa.MetaData()
    food_log = sa.Table("food_log", metadata, autoload_with=bind)
    storage_object = sa.Table("storage_object", metadata, autoload_with=bind)
    wellness_event = sa.Table("wellness_event", metadata, autoload_with=bind)
    retention_policy = sa.Table(
        "retention_policy", metadata, autoload_with=bind
    )
    events = _event_table()
    policies = _retention_policies(bind, retention_policy)
    source_rows = list(bind.execute(sa.select(food_log)).mappings())
    media_counts = Counter(
        row["media_path"] for row in source_rows if row["media_path"] is not None
    )
    media_objects = {
        row["relative_path"]: row
        for row in bind.execute(sa.select(storage_object)).mappings()
    }

    for row in source_rows:
        legacy_id = uuid.UUID(str(row["id"]))
        original = _legacy_payload(row)
        checksum = _checksum(original)
        source = str(row["source"] or "legacy_food_log")[:64]
        observed_at = _aware(row["logged_at"])
        recorded_at = _aware(row["created_at"])
        confirmed_at = max(observed_at, recorded_at)
        modality = "text"
        display_name = str(row["description"]).strip() or "Legacy meal"
        warning = "Migrated from a legacy food log without structured estimates."
        item = {
            "name": display_name,
            "intake_type": "food",
            "serving": _unknown_serving(),
            "nutrients": [],
            "confidence": "low",
            "warnings": [],
            "meal_type": row["meal_type"],
        }
        interaction_fingerprint = _fingerprint("interaction", original)
        outcome_fingerprint = _fingerprint("outcome", original)
        snapshot = {
            "interaction_id": str(legacy_id),
            "intent": "log_consumed",
            "modality": modality,
            "observed_at": _iso(observed_at),
            "timezone": "UTC",
            "source": source,
            "nutrition_observation_id": None,
            "items": [item],
            "nutrition_review_id": None,
            "analysis_provenance": None,
            "warnings": [],
            "schema_version": "structured-intake-snapshot-v1",
        }
        raw_object_id = None
        media_path = row["media_path"]
        storage_object_before = None
        if media_path is not None and media_path in media_objects:
            obj = media_objects[media_path]
            storage_object_before = {
                "id": str(uuid.UUID(str(obj["id"]))),
                "expires_at": _optional_iso(obj["expires_at"]),
                "retention_basis_at": _optional_iso(obj["retention_basis_at"]),
                "safe_to_purge": bool(obj["safe_to_purge"]),
            }
            if media_counts[media_path] == 1:
                raw_object_id = uuid.UUID(str(obj["id"]))

        def common(policy_name: str, basis: datetime) -> dict[str, Any]:
            policy = policies[policy_name]
            return {
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "observed_at": row["logged_at"],
                "recorded_at": row["created_at"],
                "timezone": "UTC",
                "source_device": source,
                "quality_flags": None,
                "coverage": None,
                "sensitivity": "wellness",
                "consent_scope": "personal",
                "retention_policy_id": uuid.UUID(str(policy["id"])),
                "expires_at": _expiry(policy, basis),
            }

        archive_common = common("legacy_food_log_archive", observed_at)
        structured_common = common("nutrition_observation", observed_at)
        raw_common = common("nutrition_raw_capture", observed_at)
        confirmation_common = common("nutrition_confirmation", confirmed_at)
        derived = {
            "legacy_food_log_id": str(legacy_id),
            "legacy_checksum": checksum,
        }
        bind.execute(
            sa.insert(events),
            [
                {
                    **archive_common,
                    "id": _uuid("archive-event", legacy_id),
                    "event_type": "legacy.food-log.v1",
                    "schema_version": 1,
                    "source_provider": ARCHIVE_PROVIDER,
                    "source_record_id": str(legacy_id),
                    "capture_method": "migration",
                    "confidence": 1.0,
                    "payload": {
                        "original": original,
                        "sha256": checksum,
                        "storage_object_before": storage_object_before,
                    },
                    "raw_object_id": None,
                    "derived_from": None,
                },
                {
                    **structured_common,
                    "id": _uuid("interaction-event", legacy_id),
                    "event_type": "nutrition.interaction.v1",
                    "schema_version": 1,
                    "source_provider": "nutrition-interaction",
                    "source_record_id": str(legacy_id),
                    "capture_method": modality,
                    "confidence": None,
                    "payload": {
                        "interaction_id": str(legacy_id),
                        "operation_fingerprint": interaction_fingerprint,
                        "intent": "log_consumed",
                        "modality": modality,
                        "observed_at": _iso(observed_at),
                        "recorded_at": _iso(recorded_at),
                        "timezone": "UTC",
                        "source": source,
                        "source_text": None,
                        "media_path": None,
                        "nutrition_observation_id": None,
                        "items": [item],
                        "nutrition_review_id": None,
                        "analysis_provenance": None,
                        "warnings": [],
                        "schema_version": "intake-interaction-v1",
                    },
                    "raw_object_id": None,
                    "derived_from": derived,
                },
                {
                    **raw_common,
                    "id": _uuid("raw-event", legacy_id),
                    "event_type": "nutrition.raw-capture.v1",
                    "schema_version": 1,
                    "source_provider": "nutrition-raw-capture",
                    "source_record_id": str(legacy_id),
                    "capture_method": modality,
                    "confidence": None,
                    "payload": {
                        "operation_fingerprint": interaction_fingerprint,
                        "source_text": row["description"],
                        "media_path": media_path,
                        "warnings": [warning],
                        "item_warnings": [[warning]],
                    },
                    "raw_object_id": raw_object_id,
                    "derived_from": {
                        **derived,
                        "interaction_id": str(legacy_id),
                    },
                },
                {
                    **structured_common,
                    "id": _uuid("operation-event", legacy_id),
                    "event_type": "nutrition.operation.v1",
                    "schema_version": 1,
                    "source_provider": "nutrition-operation",
                    "source_device": None,
                    "source_record_id": f"interaction:{legacy_id}",
                    "capture_method": "system",
                    "confidence": None,
                    "payload": {
                        "operation_kind": "intake_interaction",
                        "operation_id": str(legacy_id),
                        "operation_fingerprint": interaction_fingerprint,
                        "operation_state": "completed",
                    },
                    "raw_object_id": None,
                    "derived_from": derived,
                },
                {
                    **confirmation_common,
                    "id": _uuid("outcome-event", legacy_id),
                    "event_type": "nutrition.intake-outcome.v1",
                    "schema_version": 1,
                    "recorded_at": confirmed_at,
                    "source_provider": "nutrition-intake-outcome",
                    "source_record_id": str(_uuid("outcome", legacy_id)),
                    "capture_method": "migration",
                    "confidence": 1.0,
                    "payload": {
                        "outcome_id": str(_uuid("outcome", legacy_id)),
                        "operation_fingerprint": outcome_fingerprint,
                        "interaction_id": str(legacy_id),
                        "status": "consumed",
                        "confirmed_at": _iso(confirmed_at),
                        "source": source,
                        "consumed_at": _iso(observed_at),
                        "corrected_items": [],
                        "note": None,
                        "intake_snapshot": snapshot,
                        "schema_version": "intake-outcome-v1",
                    },
                    "raw_object_id": None,
                    "derived_from": {
                        **derived,
                        "interaction_id": str(legacy_id),
                    },
                },
            ],
        )

    archives = list(
        bind.execute(
            sa.select(wellness_event).where(
                wellness_event.c.source_provider == ARCHIVE_PROVIDER
            )
        ).mappings()
    )
    source_checksums = {
        str(uuid.UUID(str(row["id"]))): _checksum(_legacy_payload(row))
        for row in source_rows
    }
    archive_checksums = {
        str(uuid.UUID(str(row["source_record_id"]))): row["payload"]["sha256"]
        for row in archives
    }
    if archive_checksums != source_checksums:
        raise RuntimeError("FoodLog migration checksum verification failed")


def downgrade() -> None:
    if context.is_offline_mode():
        op.execute("-- FoodLog backfill cleanup requires an online downgrade.")
        return
    bind = op.get_bind()
    metadata = sa.MetaData()
    wellness_event = sa.Table("wellness_event", metadata, autoload_with=bind)
    storage_object = sa.Table("storage_object", metadata, autoload_with=bind)
    archives = list(
        bind.execute(
            sa.select(wellness_event).where(
                wellness_event.c.source_provider == ARCHIVE_PROVIDER
            )
        ).mappings()
    )
    restored_storage_ids: set[str] = set()
    for row in archives:
        before = (row["payload"] or {}).get("storage_object_before")
        if not before or before["id"] in restored_storage_ids:
            continue
        restored_storage_ids.add(before["id"])
        bind.execute(
            sa.update(storage_object)
            .where(
                storage_object.c.id
                == _database_uuid(before["id"], bind.dialect.name)
            )
            .values(
                expires_at=(
                    datetime.fromisoformat(before["expires_at"])
                    if before["expires_at"] is not None
                    else None
                ),
                retention_basis_at=(
                    datetime.fromisoformat(before["retention_basis_at"])
                    if before["retention_basis_at"] is not None
                    else None
                ),
                safe_to_purge=bool(before["safe_to_purge"]),
            )
        )
    migrated_ids = [
        row["id"]
        for row in bind.execute(sa.select(wellness_event)).mappings()
        if row["source_provider"] == ARCHIVE_PROVIDER
        or (row["derived_from"] or {}).get("legacy_food_log_id")
    ]
    if migrated_ids:
        bind.execute(
            sa.delete(wellness_event).where(wellness_event.c.id.in_(migrated_ids))
        )
