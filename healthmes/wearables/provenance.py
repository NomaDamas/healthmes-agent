"""Retained local snapshots of normalized Open Wearables context."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.storage.service import (
    DEFAULT_RETENTION,
    RETENTION_PRESETS,
    ensure_default_policies,
)
from healthmes.store import RetentionPolicy, WellnessEvent
from healthmes.store.session import session_scope
from healthmes.timezones import parse_timezone

OPEN_WEARABLES_SNAPSHOT_EVENT_TYPE = "wearable.open-wearables-snapshot.v1"
OPEN_WEARABLES_OBSERVATION_EVENT_TYPE = (
    "wearable.open-wearables-observation.v1"
)
OPEN_WEARABLES_QUERY_EVENT_TYPE = "wearable.open-wearables-query.v1"
OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER = "healthmes-open-wearables-mirror"
OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS = "wearable_normalized"

_SNAPSHOT_SCHEMA = "healthmes.open-wearables-snapshot.v1"
_OBSERVATION_SCHEMA = "healthmes.open-wearables-observation.v1"
_QUERY_SCHEMA = "healthmes.open-wearables-query.v1"
_RETENTION_BINDING_SCHEMA = "healthmes.retention-policy-binding.v1"
_MAX_CONTEXT_BYTES = 1_000_000
_MAX_PAYLOAD_BYTES = 2_000_000
_MAX_QUERY_RESULT_BYTES = 220_000
_MAX_QUERY_ROWS = 250
_MAX_RETAINED_QUERY_CANDIDATES = 32
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 20_000
_MAX_CLOCK_SKEW = timedelta(minutes=5)
_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "authtoken",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretaccesskey",
        "setcookie",
        "token",
        "xopenwearablesapikey",
    }
)
_UPSTREAM_PROVENANCE_KEYS = (
    "source_refs",
    "evidence_ids",
)
_PRIVATE_IDENTITY_KEYS = frozenset(
    {
        "accountid",
        "connectionid",
        "datasourceid",
        "deviceid",
        "externaluserid",
        "recordid",
        "userid",
    }
)


@dataclass(frozen=True, slots=True)
class WearableSnapshot:
    """A retained local snapshot suitable for fallback and SourceRef creation."""

    event_id: uuid.UUID
    content_event_id: uuid.UUID
    normalized_context: dict[str, Any]
    local_day: date
    timezone: str
    collected_at: datetime
    observed_start: datetime
    observed_end: datetime
    content_digest: str
    coverage: float | None

    def is_stale(self, *, now: datetime, max_age: timedelta) -> bool:
        """Return whether the last successful collection is older than ``max_age``."""
        if max_age < timedelta(0):
            raise ValueError("max_age must not be negative")
        return _aware_utc(now, field="now") - self.collected_at > max_age


@dataclass(frozen=True, slots=True)
class WearableQuerySnapshot:
    """One bounded, sanitized Open Wearables query retained by HealthMes."""

    event_id: uuid.UUID
    capability: str
    query_digest: str
    result: dict[str, Any]
    start: datetime
    end: datetime
    timezone: str
    collected_at: datetime
    retention_basis_at: datetime
    coverage: float | None


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive UTC reads and aware PostgreSQL reads."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _secret_key(value: str) -> bool:
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    return any(
        normalized == secret or normalized.endswith(secret)
        for secret in _SECRET_KEYS
    )


def _reject_secret_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _secret_key(str(key)):
                raise ValueError(f"wearable snapshot contains a secret field at {path}")
            _reject_secret_keys(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, item in enumerate(value):
            _reject_secret_keys(item, path=f"{path}[{index}]")


def _reject_private_identity_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = "".join(
                character
                for character in str(key).casefold()
                if character.isalnum()
            )
            if normalized == "id" or any(
                normalized == identity
                or normalized.endswith(identity)
                for identity in _PRIVATE_IDENTITY_KEYS
            ):
                raise ValueError(
                    f"wearable snapshot contains a private identity at {path}"
                )
            _reject_private_identity_keys(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, item in enumerate(value):
            _reject_private_identity_keys(item, path=f"{path}[{index}]")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalize_json(value: Any, *, max_bytes: int) -> Any:
    """Detach an exact-JSON tree and enforce bounded canonical storage."""
    nodes_seen = 0
    active_containers: set[int] = set()

    def normalize(item: Any, *, depth: int) -> Any:
        nonlocal nodes_seen
        nodes_seen += 1
        if nodes_seen > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("JSON value exceeds normalization limits")
        item_type = type(item)
        if item is None or item_type in {str, bool, int}:
            return item
        if item_type is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return item
        if item_type is list:
            container_id = id(item)
            if container_id in active_containers:
                raise ValueError("JSON containers must not be cyclic")
            active_containers.add(container_id)
            try:
                return [
                    normalize(child, depth=depth + 1)
                    for child in list.__iter__(item)
                ]
            finally:
                active_containers.discard(container_id)
        if item_type is dict:
            container_id = id(item)
            if container_id in active_containers:
                raise ValueError("JSON containers must not be cyclic")
            active_containers.add(container_id)
            try:
                normalized: dict[str, Any] = {}
                for key, child in dict.items(item):
                    if type(key) is not str:
                        raise TypeError("JSON object keys must be strings")
                    normalized[key] = normalize(
                        child,
                        depth=depth + 1,
                    )
                return normalized
            finally:
                active_containers.discard(container_id)
        raise TypeError("JSON value must use exact built-in types")

    normalized = normalize(value, depth=0)
    if len(_canonical_json(normalized)) > max_bytes:
        raise ValueError("JSON value exceeds encoded size limit")
    return normalized


def _local_day_bounds(
    local_day: date,
    timezone: str,
) -> tuple[datetime, datetime]:
    zone = parse_timezone(timezone)
    start = datetime.combine(local_day, time.min, tzinfo=zone)
    end = datetime.combine(
        local_day + timedelta(days=1),
        time.min,
        tzinfo=zone,
    )
    return start.astimezone(UTC), end.astimezone(UTC)


def _normalize_context(
    normalized_context: dict[str, Any],
    *,
    local_day: date,
    timezone: str,
) -> tuple[dict[str, Any], bytes]:
    if type(normalized_context) is not dict:
        raise TypeError("normalized_context must be a JSON object")
    normalized = _normalize_json(
        normalized_context,
        max_bytes=_MAX_CONTEXT_BYTES,
    )
    assert isinstance(normalized, dict)
    _reject_secret_keys(normalized)

    context_day = normalized.get("date")
    if context_day is not None and context_day != local_day.isoformat():
        raise ValueError("normalized context date does not match local_day")
    context_timezone = normalized.get("timezone")
    if context_timezone is not None and context_timezone != timezone:
        raise ValueError("normalized context timezone does not match timezone")
    return normalized, _canonical_json(normalized)


def _retention_policy(session: Session) -> RetentionPolicy:
    policies = {
        policy.data_class: policy
        for policy in ensure_default_policies(session)
    }
    return policies[OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS]


def _retention_policy_binding(
    *,
    enabled: bool,
    retention_days: int | None,
) -> dict[str, Any]:
    state = {
        "data_class": OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "enabled": enabled,
        "retention_days": retention_days,
    }
    revision = hashlib.sha256(
        _canonical_json(
            {
                "schema": _RETENTION_BINDING_SCHEMA,
                **state,
            }
        )
    ).hexdigest()
    return {
        **state,
        "revision": f"sha256:{revision}",
    }


def open_wearables_retention_policy_binding(
    session: Session,
) -> dict[str, Any]:
    """Return the semantic retention revision without mutating read sessions."""

    policy_state = session.execute(
        select(
            RetentionPolicy.enabled,
            RetentionPolicy.retention_days,
        ).where(
            RetentionPolicy.data_class
            == OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS
        )
    ).one_or_none()
    if policy_state is not None:
        enabled, retention_days = policy_state
        return _retention_policy_binding(
            enabled=bool(enabled),
            retention_days=retention_days,
        )
    preset = DEFAULT_RETENTION.get(
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS
    )
    return _retention_policy_binding(
        enabled=preset is not None,
        retention_days=(
            RETENTION_PRESETS[preset] if preset is not None else None
        ),
    )


def _expiry(
    policy: RetentionPolicy,
    *,
    observed_at: datetime,
) -> datetime | None:
    if not policy.enabled or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def _context_coverage(normalized_context: Mapping[str, Any]) -> float | None:
    value = normalized_context.get("coverage")
    if isinstance(value, Mapping):
        value = value.get("ratio")
    if (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 <= float(value) <= 1
    ):
        return float(value)
    return None


def _source_record_id(
    *,
    local_day: date,
    content_digest: str,
) -> str:
    return f"snapshot:{local_day.isoformat()}:{content_digest}"


def _observation_source_record_id(
    *,
    local_day: date,
    timezone: str,
    collected_at: datetime,
    content_digest: str,
) -> str:
    timezone_digest = hashlib.sha256(timezone.encode("utf-8")).hexdigest()[:12]
    return (
        f"observation:{local_day.isoformat()}:{timezone_digest}:"
        f"{collected_at.isoformat()}:{content_digest}"
    )


def _content_digest(
    *,
    normalized_context: dict[str, Any],
    local_day: date,
    timezone: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "local_day": local_day.isoformat(),
                "normalized_context": normalized_context,
                "schema": _SNAPSHOT_SCHEMA,
                "timezone": timezone,
            }
        )
    ).hexdigest()


def _snapshot_payload(
    *,
    normalized_context: dict[str, Any],
    local_day: date,
    timezone: str,
    collected_at: datetime,
    observed_start: datetime,
    observed_end: datetime,
    content_digest: str,
) -> dict[str, Any]:
    provenance = {
        key: normalized_context[key]
        for key in _UPSTREAM_PROVENANCE_KEYS
        if key in normalized_context
    }
    payload = {
        "schema": _SNAPSHOT_SCHEMA,
        "local_day": local_day.isoformat(),
        "timezone": timezone,
        "collected_at": collected_at.isoformat(),
        "window": {
            "start": observed_start.isoformat(),
            "end": observed_end.isoformat(),
        },
        "content_digest": content_digest,
        "normalized_context": normalized_context,
        "upstream_provenance": provenance,
    }
    return _normalize_json(
        payload,
        max_bytes=_MAX_PAYLOAD_BYTES,
    )


def _observation_payload(
    *,
    snapshot_event_id: uuid.UUID,
    local_day: date,
    timezone: str,
    collected_at: datetime,
    observed_start: datetime,
    observed_end: datetime,
    content_digest: str,
) -> dict[str, Any]:
    return _normalize_json(
        {
            "schema": _OBSERVATION_SCHEMA,
            "snapshot_event_id": str(snapshot_event_id),
            "local_day": local_day.isoformat(),
            "timezone": timezone,
            "collected_at": collected_at.isoformat(),
            "window": {
                "start": observed_start.isoformat(),
                "end": observed_end.isoformat(),
            },
            "content_digest": content_digest,
        },
        max_bytes=_MAX_PAYLOAD_BYTES,
    )


def _validate_existing_snapshot(
    event: WellnessEvent,
    *,
    source_record_id: str,
    timezone: str,
    observed_start: datetime,
    content_digest: str,
    coverage: float | None,
    now: datetime,
) -> None:
    snapshot = _content_from_event(event)
    if (
        snapshot is None
        or event.source_record_id != source_record_id
        or snapshot.timezone != timezone
        or snapshot.observed_start != observed_start
        or snapshot.content_digest != content_digest
        or snapshot.coverage != coverage
        or event.raw_object_id is not None
        or (
            event.expires_at is not None
            and _database_utc(event.expires_at) <= now
        )
    ):
        raise ValueError("stored wearable snapshot identity has conflicting content")


def persist_open_wearables_observation(
    session: Session,
    *,
    normalized_context: dict[str, Any],
    local_day: date,
    timezone: str,
    collected_at: datetime,
    now: datetime,
) -> WearableSnapshot:
    """Persist one observation under the shared wellness write fence."""
    with activity_write_lock():
        lock_activity_write_plane(session)
        return _persist_open_wearables_observation(
            session,
            normalized_context=normalized_context,
            local_day=local_day,
            timezone=timezone,
            collected_at=collected_at,
            now=now,
        )


def _persist_open_wearables_observation(
    session: Session,
    *,
    normalized_context: dict[str, Any],
    local_day: date,
    timezone: str,
    collected_at: datetime,
    now: datetime,
) -> WearableSnapshot:
    """Flush immutable content plus one collection observation.

    The caller owns the transaction. Repeated content reuses one immutable
    content event, while every distinct collection time gets an append-only
    observation event so A -> B -> A remains ordered correctly.
    """
    if type(local_day) is not date:
        raise TypeError("local_day must be a date")
    current = _aware_utc(now, field="now")
    collected = _aware_utc(collected_at, field="collected_at")
    if collected > current + _MAX_CLOCK_SKEW:
        raise ValueError("collected_at is too far in the future")
    parse_timezone(timezone)
    observed_start, observed_end = _local_day_bounds(local_day, timezone)
    context, canonical_context = _normalize_context(
        normalized_context,
        local_day=local_day,
        timezone=timezone,
    )
    content_digest = _content_digest(
        normalized_context=context,
        local_day=local_day,
        timezone=timezone,
    )
    source_record_id = _source_record_id(
        local_day=local_day,
        content_digest=content_digest,
    )
    payload = _snapshot_payload(
        normalized_context=context,
        local_day=local_day,
        timezone=timezone,
        collected_at=collected,
        observed_start=observed_start,
        observed_end=observed_end,
        content_digest=content_digest,
    )
    coverage = _context_coverage(context)

    content_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider
            == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
            WellnessEvent.source_record_id == source_record_id,
        )
    )
    if content_event is not None:
        _validate_existing_snapshot(
            content_event,
            source_record_id=source_record_id,
            timezone=timezone,
            observed_start=observed_start,
            content_digest=content_digest,
            coverage=coverage,
            now=current,
        )
    else:
        policy = _retention_policy(session)
        expires_at = _expiry(policy, observed_at=observed_start)
        if expires_at is not None and expires_at <= current:
            raise ValueError(
                "local_day falls outside the normalized retention window"
            )
        content_event = WellnessEvent(
            event_type=OPEN_WEARABLES_SNAPSHOT_EVENT_TYPE,
            schema_version=1,
            observed_at=observed_start,
            recorded_at=collected,
            timezone=timezone,
            source_provider=OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
            source_device=None,
            source_record_id=source_record_id,
            capture_method="import",
            quality_flags={"content_digest": content_digest},
            confidence=None,
            coverage=coverage,
            sensitivity="wearable",
            consent_scope="personal",
            retention_policy_id=policy.id,
            expires_at=expires_at,
            payload=payload,
            raw_object_id=None,
            derived_from={
                "source": "open-wearables",
                "canonical_context_bytes": len(canonical_context),
            },
        )
        try:
            with session.begin_nested():
                session.add(content_event)
                session.flush([content_event])
        except IntegrityError:
            concurrent = session.scalar(
                select(WellnessEvent)
                .where(
                    WellnessEvent.source_provider
                    == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
                    WellnessEvent.source_record_id == source_record_id,
                )
                .with_for_update()
            )
            if concurrent is None:
                raise
            _validate_existing_snapshot(
                concurrent,
                source_record_id=source_record_id,
                timezone=timezone,
                observed_start=observed_start,
                content_digest=content_digest,
                coverage=coverage,
                now=current,
            )
            content_event = concurrent

    observation_source_record_id = _observation_source_record_id(
        local_day=local_day,
        timezone=timezone,
        collected_at=collected,
        content_digest=content_digest,
    )
    observation_payload = _observation_payload(
        snapshot_event_id=content_event.id,
        local_day=local_day,
        timezone=timezone,
        collected_at=collected,
        observed_start=observed_start,
        observed_end=observed_end,
        content_digest=content_digest,
    )
    observation = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider
            == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
            WellnessEvent.source_record_id
            == observation_source_record_id,
        )
    )
    if observation is None:
        policy = _retention_policy(session)
        expires_at = _expiry(policy, observed_at=observed_start)
        if expires_at is not None and expires_at <= current:
            raise ValueError(
                "local_day falls outside the normalized retention window"
            )
        observation = WellnessEvent(
            event_type=OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
            schema_version=1,
            observed_at=observed_start,
            recorded_at=collected,
            timezone=timezone,
            source_provider=OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
            source_device=None,
            source_record_id=observation_source_record_id,
            capture_method="import",
            quality_flags={
                "content_digest": content_digest,
                "snapshot_event_id": str(content_event.id),
            },
            confidence=None,
            coverage=coverage,
            sensitivity="wearable",
            consent_scope="personal",
            retention_policy_id=policy.id,
            expires_at=expires_at,
            payload=observation_payload,
            raw_object_id=None,
            derived_from={
                "source": "open-wearables",
                "snapshot_event_id": str(content_event.id),
            },
        )
        try:
            with session.begin_nested():
                session.add(observation)
                session.flush([observation])
        except IntegrityError:
            concurrent = session.scalar(
                select(WellnessEvent)
                .where(
                    WellnessEvent.source_provider
                    == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
                    WellnessEvent.source_record_id
                    == observation_source_record_id,
                )
                .with_for_update()
            )
            if concurrent is None:
                raise
            observation = concurrent

    snapshot = _snapshot_from_observation(
        session,
        observation,
        now=current,
    )
    if snapshot is None:
        raise ValueError("stored wearable observation failed validation")
    return snapshot


def persist_open_wearables_snapshot(
    session: Session,
    *,
    normalized_context: dict[str, Any],
    local_day: date,
    timezone: str,
    collected_at: datetime,
    now: datetime,
) -> WellnessEvent:
    """Compatibility wrapper returning the immutable content event.

    Every call still records a collection observation. New decision code
    should use :func:`persist_open_wearables_observation` so provenance points
    at the actual collection occurrence rather than the deduplicated content.
    """
    snapshot = persist_open_wearables_observation(
        session,
        normalized_context=normalized_context,
        local_day=local_day,
        timezone=timezone,
        collected_at=collected_at,
        now=now,
    )
    event = session.get(WellnessEvent, snapshot.content_event_id)
    if event is None:
        raise ValueError("persisted wearable content disappeared")
    for field in ("observed_at", "recorded_at", "expires_at"):
        value = getattr(event, field)
        if value is not None:
            setattr(event, field, _database_utc(value))
    return event


def commit_open_wearables_snapshot(
    session_factory: sessionmaker[Session],
    *,
    normalized_context: dict[str, Any],
    local_day: date,
    timezone: str,
    collected_at: datetime,
    now: datetime,
) -> WearableSnapshot:
    """Persist one immutable snapshot in its own commit-on-success transaction."""
    with session_scope(session_factory) as session:
        bind = session.get_bind()
        if isinstance(bind.pool, StaticPool):
            raise RuntimeError(
                "independent wearable commits require a distinct physical "
                "database connection"
            )
        return persist_open_wearables_observation(
            session,
            normalized_context=normalized_context,
            local_day=local_day,
            timezone=timezone,
            collected_at=collected_at,
            now=now,
        )


def _normalize_query_scope(
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    normalized_capability = capability.strip().casefold()
    if (
        not normalized_capability
        or len(normalized_capability) > 128
        or not normalized_capability.startswith("wearable.")
    ):
        raise ValueError("invalid wearable query capability")
    observed_start = _aware_utc(start, field="start")
    observed_end = _aware_utc(end, field="end")
    if observed_end <= observed_start:
        raise ValueError("wearable query end must be after start")
    parse_timezone(timezone)
    normalized_parameters = _normalize_json(
        {
            str(key): value
            for key, value in parameters.items()
            if key not in {"cursor", "date"}
        },
        max_bytes=16_000,
    )
    assert isinstance(normalized_parameters, dict)
    _reject_secret_keys(normalized_parameters)
    _reject_private_identity_keys(normalized_parameters)
    scope = {
        "capability": normalized_capability,
        "start": observed_start.isoformat(),
        "end": observed_end.isoformat(),
        "timezone": timezone,
        "parameters": normalized_parameters,
    }
    query_digest = hashlib.sha256(
        _canonical_json(
            {
                "schema": _QUERY_SCHEMA,
                "query": scope,
            }
        )
    ).hexdigest()
    return scope, query_digest


def open_wearables_query_digest(
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    parameters: Mapping[str, Any],
) -> str:
    """Return the stable identity for one bounded wearable query."""

    _scope, query_digest = _normalize_query_scope(
        capability=capability,
        start=start,
        end=end,
        timezone=timezone,
        parameters=parameters,
    )
    return query_digest


def _normalize_query_result(result: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_json(
        dict(result),
        max_bytes=_MAX_QUERY_RESULT_BYTES,
    )
    assert isinstance(normalized, dict)
    _reject_secret_keys(normalized)
    _reject_private_identity_keys(normalized)
    records = normalized.get("records")
    if (
        not isinstance(records, list)
        or len(records) > _MAX_QUERY_ROWS
    ):
        raise ValueError("wearable query result records are invalid")
    return normalized


def _query_source_record_id(
    *,
    query_digest: str,
    collected_at: datetime,
    result_digest: str,
) -> str:
    observation_digest = hashlib.sha256(
        _canonical_json(
            {
                "collected_at": collected_at.isoformat(),
                "query_digest": query_digest,
                "result_digest": result_digest,
            }
        )
    ).hexdigest()
    return f"query:{query_digest}:{observation_digest}"


def _query_retention_basis(
    result: Mapping[str, Any],
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    collected_at: datetime,
) -> datetime:
    """Use the oldest retained record, not the requested window, for expiry."""

    observed_start = _aware_utc(start, field="start")
    observed_end = _aware_utc(end, field="end")
    collected = _aware_utc(collected_at, field="collected_at")
    records = result.get("records")
    if not isinstance(records, list):
        raise ValueError("wearable query result records are invalid")
    if not records:
        return collected

    zone = parse_timezone(timezone)
    observations: list[datetime] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError(
                "wearable query result record is invalid"
            )
        observation: datetime | None = None
        raw_timestamp: Any = None
        if (
            isinstance(record.get("summary_kind"), str)
            and isinstance(record.get("date"), str)
        ):
            try:
                observed_day = date.fromisoformat(str(record["date"]))
            except ValueError:
                observed_day = None
            if observed_day is not None:
                observation = datetime.combine(
                    observed_day,
                    time.min,
                    tzinfo=zone,
                ).astimezone(UTC)
                observation = max(observation, observed_start)
        else:
            raw_timestamp = next(
                (
                    record.get(field)
                    for field in (
                        "timestamp",
                        "recorded_at",
                        "start_time",
                    )
                    if record.get(field) is not None
                ),
                None,
            )
        if observation is None and isinstance(raw_timestamp, str):
            try:
                parsed = datetime.fromisoformat(
                    raw_timestamp.replace("Z", "+00:00")
                )
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is not None:
                observation = parsed.astimezone(UTC)
        elif observation is None and isinstance(record.get("date"), str):
            try:
                observed_day = date.fromisoformat(str(record["date"]))
            except ValueError:
                observed_day = None
            if observed_day is not None:
                observation = datetime.combine(
                    observed_day,
                    time.min,
                    tzinfo=zone,
                ).astimezone(UTC)
                observation = max(observation, observed_start)
        if (
            observation is None
            or observation < observed_start
            or observation >= observed_end
        ):
            raise ValueError(
                "wearable query result record observation is invalid"
            )
        if capability == "wearable.workouts":
            raw_end = record.get("end_time")
            if not isinstance(raw_end, str):
                raise ValueError(
                    "wearable workout result interval is invalid"
                )
            try:
                workout_end = datetime.fromisoformat(
                    raw_end.replace("Z", "+00:00")
                )
            except ValueError:
                workout_end = None
            if (
                workout_end is None
                or workout_end.tzinfo is None
                or not observation
                < workout_end.astimezone(UTC)
                <= observed_end
            ):
                raise ValueError(
                    "wearable workout result interval is invalid"
                )
        observations.append(observation)
    return min(observations)


def _query_payload(
    *,
    scope: Mapping[str, Any],
    query_digest: str,
    result: Mapping[str, Any],
    result_digest: str,
    collected_at: datetime,
    retention_basis_at: datetime,
) -> dict[str, Any]:
    return _normalize_json(
        {
            "schema": _QUERY_SCHEMA,
            "query": dict(scope),
            "query_digest": query_digest,
            "result": dict(result),
            "result_digest": result_digest,
            "collected_at": collected_at.isoformat(),
            "retention_basis_at": retention_basis_at.isoformat(),
            "window": {
                "start": scope["start"],
                "end": scope["end"],
            },
        },
        max_bytes=_MAX_PAYLOAD_BYTES,
    )


def persist_open_wearables_query_snapshot(
    session: Session,
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    parameters: Mapping[str, Any],
    result: Mapping[str, Any],
    collected_at: datetime,
    now: datetime,
) -> WearableQuerySnapshot:
    """Persist one bounded sanitized query result under the wellness fence."""

    with activity_write_lock():
        lock_activity_write_plane(session)
        current = _aware_utc(now, field="now")
        collected = _aware_utc(collected_at, field="collected_at")
        if collected > current + _MAX_CLOCK_SKEW:
            raise ValueError("collected_at is too far in the future")
        scope, query_digest = _normalize_query_scope(
            capability=capability,
            start=start,
            end=end,
            timezone=timezone,
            parameters=parameters,
        )
        normalized_result = _normalize_query_result(result)
        retention_basis_at = _query_retention_basis(
            normalized_result,
            capability=capability,
            start=datetime.fromisoformat(str(scope["start"])),
            end=datetime.fromisoformat(str(scope["end"])),
            timezone=timezone,
            collected_at=collected,
        )
        result_digest = hashlib.sha256(
            _canonical_json(normalized_result)
        ).hexdigest()
        source_record_id = _query_source_record_id(
            query_digest=query_digest,
            collected_at=collected,
            result_digest=result_digest,
        )
        payload = _query_payload(
            scope=scope,
            query_digest=query_digest,
            result=normalized_result,
            result_digest=result_digest,
            collected_at=collected,
            retention_basis_at=retention_basis_at,
        )
        coverage = _context_coverage(normalized_result)
        policy = _retention_policy(session)
        retention_window = normalized_result.get("retention_window")
        stored_policy_binding = (
            retention_window.get("retention_policy")
            if isinstance(retention_window, Mapping)
            else None
        )
        expected_policy_binding = open_wearables_retention_policy_binding(
            session
        )
        if (
            stored_policy_binding is not None
            and stored_policy_binding != expected_policy_binding
        ):
            raise ValueError(
                "wearable retention policy changed before snapshot commit"
            )
        event = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_provider
                == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
                WellnessEvent.source_record_id == source_record_id,
            )
        )
        if event is None:
            retention_days = expected_policy_binding["retention_days"]
            expires_at = (
                retention_basis_at + timedelta(days=retention_days)
                if expected_policy_binding["enabled"]
                and retention_days is not None
                else None
            )
            if expires_at is not None and expires_at <= current:
                raise ValueError(
                    "wearable query falls outside the normalized "
                    "retention window"
                )
            event = WellnessEvent(
                event_type=OPEN_WEARABLES_QUERY_EVENT_TYPE,
                schema_version=1,
                observed_at=retention_basis_at,
                recorded_at=collected,
                timezone=timezone,
                source_provider=OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
                source_device=None,
                source_record_id=source_record_id,
                capture_method="import",
                quality_flags={
                    "query_digest": query_digest,
                    "result_digest": result_digest,
                },
                confidence=None,
                coverage=coverage,
                sensitivity="wearable",
                consent_scope="personal",
                retention_policy_id=policy.id,
                expires_at=expires_at,
                payload=payload,
                raw_object_id=None,
                derived_from={
                    "source": "open-wearables",
                    "mode": "bounded-query-mirror",
                    "query_digest": query_digest,
                },
            )
            try:
                with session.begin_nested():
                    session.add(event)
                    session.flush([event])
            except IntegrityError:
                concurrent = session.scalar(
                    select(WellnessEvent)
                    .where(
                        WellnessEvent.source_provider
                        == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
                        WellnessEvent.source_record_id
                        == source_record_id,
                    )
                    .with_for_update()
                )
                if concurrent is None:
                    raise
                event = concurrent
        snapshot = wearable_query_snapshot_from_event(
            session,
            event,
            now=current,
        )
        if snapshot is None:
            raise ValueError("stored wearable query snapshot failed validation")
        return snapshot


def commit_open_wearables_query_snapshot(
    session_factory: sessionmaker[Session],
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    parameters: Mapping[str, Any],
    result: Mapping[str, Any],
    collected_at: datetime,
    now: datetime,
) -> WearableQuerySnapshot:
    """Commit one query mirror independently from a read-only search session."""

    with session_scope(session_factory) as session:
        bind = session.get_bind()
        if isinstance(bind.pool, StaticPool):
            raise RuntimeError(
                "independent wearable commits require a distinct physical "
                "database connection"
            )
        return persist_open_wearables_query_snapshot(
            session,
            capability=capability,
            start=start,
            end=end,
            timezone=timezone,
            parameters=parameters,
            result=result,
            collected_at=collected_at,
            now=now,
        )


def wearable_query_snapshot_from_event(
    session: Session,
    event: WellnessEvent,
    *,
    now: datetime,
) -> WearableQuerySnapshot | None:
    """Validate and detach one retained bounded query event."""

    current = _aware_utc(now, field="now")
    try:
        payload = event.payload
        if not isinstance(payload, Mapping):
            return None
        scope = payload["query"]
        result = payload["result"]
        if not isinstance(scope, Mapping) or not isinstance(result, Mapping):
            return None
        capability = str(scope["capability"])
        timezone = str(scope["timezone"])
        start = _aware_utc(
            datetime.fromisoformat(str(scope["start"])),
            field="query.start",
        )
        end = _aware_utc(
            datetime.fromisoformat(str(scope["end"])),
            field="query.end",
        )
        parameters = scope["parameters"]
        if not isinstance(parameters, Mapping):
            return None
        expected_scope, query_digest = _normalize_query_scope(
            capability=capability,
            start=start,
            end=end,
            timezone=timezone,
            parameters=parameters,
        )
        normalized_result = _normalize_query_result(result)
        result_digest = hashlib.sha256(
            _canonical_json(normalized_result)
        ).hexdigest()
        collected_at = _aware_utc(
            datetime.fromisoformat(str(payload["collected_at"])),
            field="collected_at",
        )
        retention_basis_at = _query_retention_basis(
            normalized_result,
            capability=capability,
            start=start,
            end=end,
            timezone=timezone,
            collected_at=collected_at,
        )
        expected_source_record_id = _query_source_record_id(
            query_digest=query_digest,
            collected_at=collected_at,
            result_digest=result_digest,
        )
        expected_payload = _query_payload(
            scope=expected_scope,
            query_digest=query_digest,
            result=normalized_result,
            result_digest=result_digest,
            collected_at=collected_at,
            retention_basis_at=retention_basis_at,
        )
        if (
            end <= start
            or event.event_type != OPEN_WEARABLES_QUERY_EVENT_TYPE
            or event.schema_version != 1
            or event.source_provider
            != OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
            or event.source_record_id != expected_source_record_id
            or event.timezone != timezone
            or event.source_device is not None
            or event.capture_method != "import"
            or event.sensitivity != "wearable"
            or event.consent_scope != "personal"
            or event.raw_object_id is not None
            or _database_utc(event.observed_at) != retention_basis_at
            or _database_utc(event.recorded_at) != collected_at
            or event.coverage != _context_coverage(normalized_result)
            or event.quality_flags
            != {
                "query_digest": query_digest,
                "result_digest": result_digest,
            }
            or event.derived_from
            != {
                "source": "open-wearables",
                "mode": "bounded-query-mirror",
                "query_digest": query_digest,
            }
            or payload.get("query_digest") != query_digest
            or payload.get("result_digest") != result_digest
            or payload.get("retention_basis_at")
            != retention_basis_at.isoformat()
            or _canonical_json(payload) != _canonical_json(expected_payload)
            or (
                event.expires_at is not None
                and _database_utc(event.expires_at) <= current
            )
        ):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return WearableQuerySnapshot(
        event_id=event.id,
        capability=capability,
        query_digest=query_digest,
        result=normalized_result,
        start=start,
        end=end,
        timezone=timezone,
        collected_at=collected_at,
        retention_basis_at=retention_basis_at,
        coverage=event.coverage,
    )


def latest_retained_open_wearables_query_snapshot(
    session: Session,
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    parameters: Mapping[str, Any],
    now: datetime,
) -> WearableQuerySnapshot | None:
    """Load the newest valid mirror for one exact bounded query."""

    snapshots = retained_open_wearables_query_snapshots(
        session,
        capability=capability,
        start=start,
        end=end,
        timezone=timezone,
        parameters=parameters,
        now=now,
    )
    return snapshots[0] if snapshots else None


def retained_open_wearables_query_snapshots(
    session: Session,
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    parameters: Mapping[str, Any],
    now: datetime,
    candidate_limit: int | None = _MAX_RETAINED_QUERY_CANDIDATES,
) -> tuple[WearableQuerySnapshot, ...]:
    """Load retained mirrors for one exact query, newest first."""

    _scope, query_digest = _normalize_query_scope(
        capability=capability,
        start=start,
        end=end,
        timezone=timezone,
        parameters=parameters,
    )
    current = _aware_utc(now, field="now")
    prefix = f"query:{query_digest}:%"
    statement = (
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == OPEN_WEARABLES_QUERY_EVENT_TYPE,
            WellnessEvent.source_provider
            == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
            WellnessEvent.source_record_id.like(prefix),
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > current,
            ),
        )
        .order_by(
            WellnessEvent.recorded_at.desc(),
            WellnessEvent.created_at.desc(),
            WellnessEvent.id.desc(),
        )
    )
    if candidate_limit is not None:
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive or None")
        statement = statement.limit(candidate_limit)
    rows = session.scalars(statement)
    snapshots: list[WearableQuerySnapshot] = []
    for event in rows:
        snapshot = wearable_query_snapshot_from_event(
            session,
            event,
            now=current,
        )
        if snapshot is not None and snapshot.query_digest == query_digest:
            snapshots.append(snapshot)
    return tuple(snapshots)


def wearable_snapshot_from_event(
    session: Session,
    event: WellnessEvent,
    *,
    now: datetime,
) -> WearableSnapshot | None:
    """Validate and detach one stored collection observation."""
    return _snapshot_from_observation(
        session,
        event,
        now=_aware_utc(now, field="now"),
    )


def _content_from_event(event: WellnessEvent) -> WearableSnapshot | None:
    payload = event.payload
    try:
        local_day = date.fromisoformat(str(payload["local_day"]))
        timezone = str(payload["timezone"])
        parse_timezone(timezone)
        window = payload["window"]
        if not isinstance(window, Mapping):
            return None
        observed_start = _aware_utc(
            datetime.fromisoformat(str(window["start"])),
            field="window.start",
        )
        observed_end = _aware_utc(
            datetime.fromisoformat(str(window["end"])),
            field="window.end",
        )
        collected_at = _aware_utc(
            datetime.fromisoformat(str(payload["collected_at"])),
            field="collected_at",
        )
        normalized_context = payload["normalized_context"]
        if not isinstance(normalized_context, dict):
            return None
        normalized_context = _normalize_json(
            normalized_context,
            max_bytes=_MAX_CONTEXT_BYTES,
        )
        assert isinstance(normalized_context, dict)
        _reject_secret_keys(normalized_context)
        content_digest = str(payload["content_digest"])
        expected_digest = _content_digest(
            normalized_context=normalized_context,
            local_day=local_day,
            timezone=timezone,
        )
        expected_source_record_id = _source_record_id(
            local_day=local_day,
            content_digest=expected_digest,
        )
        expected_start, expected_end = _local_day_bounds(
            local_day,
            timezone,
        )
        expected_payload = _snapshot_payload(
            normalized_context=normalized_context,
            local_day=local_day,
            timezone=timezone,
            collected_at=collected_at,
            observed_start=expected_start,
            observed_end=expected_end,
            content_digest=expected_digest,
        )
        if (
            content_digest != expected_digest
            or event.event_type
            != OPEN_WEARABLES_SNAPSHOT_EVENT_TYPE
            or event.source_provider
            != OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
            or event.schema_version != 1
            or event.source_record_id != expected_source_record_id
            or event.timezone != timezone
            or event.source_device is not None
            or event.capture_method != "import"
            or event.sensitivity != "wearable"
            or event.consent_scope != "personal"
            or event.raw_object_id is not None
            or observed_start != expected_start
            or observed_end != expected_end
            or _database_utc(event.observed_at) != expected_start
            or _database_utc(event.recorded_at) != collected_at
            or event.coverage
            != _context_coverage(normalized_context)
            or event.quality_flags
            != {"content_digest": expected_digest}
            or event.derived_from
            != {
                "source": "open-wearables",
                "canonical_context_bytes": len(
                    _canonical_json(normalized_context)
                ),
            }
            or _canonical_json(payload) != _canonical_json(expected_payload)
        ):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return WearableSnapshot(
        event_id=event.id,
        content_event_id=event.id,
        normalized_context=normalized_context,
        local_day=local_day,
        timezone=timezone,
        collected_at=collected_at,
        observed_start=observed_start,
        observed_end=observed_end,
        content_digest=content_digest,
        coverage=event.coverage,
    )


def _snapshot_from_observation(
    session: Session,
    event: WellnessEvent,
    *,
    now: datetime,
) -> WearableSnapshot | None:
    payload = event.payload
    try:
        local_day = date.fromisoformat(str(payload["local_day"]))
        timezone = str(payload["timezone"])
        parse_timezone(timezone)
        collected_at = _aware_utc(
            datetime.fromisoformat(str(payload["collected_at"])),
            field="collected_at",
        )
        window = payload["window"]
        if not isinstance(window, Mapping):
            return None
        observed_start = _aware_utc(
            datetime.fromisoformat(str(window["start"])),
            field="window.start",
        )
        observed_end = _aware_utc(
            datetime.fromisoformat(str(window["end"])),
            field="window.end",
        )
        content_digest = str(payload["content_digest"])
        content_event_id = uuid.UUID(str(payload["snapshot_event_id"]))
        expected_start, expected_end = _local_day_bounds(
            local_day,
            timezone,
        )
        expected_source_record_id = _observation_source_record_id(
            local_day=local_day,
            timezone=timezone,
            collected_at=collected_at,
            content_digest=content_digest,
        )
        expected_payload = _observation_payload(
            snapshot_event_id=content_event_id,
            local_day=local_day,
            timezone=timezone,
            collected_at=collected_at,
            observed_start=expected_start,
            observed_end=expected_end,
            content_digest=content_digest,
        )
        if (
            event.event_type != OPEN_WEARABLES_OBSERVATION_EVENT_TYPE
            or event.source_provider
            != OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
            or event.schema_version != 1
            or event.source_record_id != expected_source_record_id
            or event.timezone != timezone
            or event.source_device is not None
            or event.capture_method != "import"
            or event.sensitivity != "wearable"
            or event.consent_scope != "personal"
            or event.raw_object_id is not None
            or observed_start != expected_start
            or observed_end != expected_end
            or _database_utc(event.observed_at) != expected_start
            or _database_utc(event.recorded_at) != collected_at
            or event.quality_flags
            != {
                "content_digest": content_digest,
                "snapshot_event_id": str(content_event_id),
            }
            or event.derived_from
            != {
                "source": "open-wearables",
                "snapshot_event_id": str(content_event_id),
            }
            or _canonical_json(payload) != _canonical_json(expected_payload)
            or (
                event.expires_at is not None
                and _database_utc(event.expires_at) <= now
            )
        ):
            return None
        content_event = session.get(WellnessEvent, content_event_id)
        if content_event is None:
            return None
        content = _content_from_event(content_event)
        if (
            content is None
            or content.content_digest != content_digest
            or content.local_day != local_day
            or content.timezone != timezone
            or content.observed_start != observed_start
            or content.observed_end != observed_end
            or content.coverage != event.coverage
            or (
                content_event.expires_at is not None
                and _database_utc(content_event.expires_at) <= now
            )
        ):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return WearableSnapshot(
        event_id=event.id,
        content_event_id=content_event_id,
        normalized_context=content.normalized_context,
        local_day=local_day,
        timezone=timezone,
        collected_at=collected_at,
        observed_start=observed_start,
        observed_end=observed_end,
        content_digest=content_digest,
        coverage=event.coverage,
    )


def latest_retained_open_wearables_snapshot(
    session: Session,
    *,
    local_day: date,
    timezone: str,
    now: datetime,
) -> WearableSnapshot | None:
    """Return the newest non-expired snapshot for one exact local-day scope."""
    current = _aware_utc(now, field="now")
    parse_timezone(timezone)
    observed_start, observed_end = _local_day_bounds(local_day, timezone)
    rows = session.scalars(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type
            == OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
            WellnessEvent.source_provider
            == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
            WellnessEvent.timezone == timezone,
            WellnessEvent.observed_at >= observed_start,
            WellnessEvent.observed_at < observed_end,
            or_(
                WellnessEvent.expires_at.is_(None),
                WellnessEvent.expires_at > current,
            ),
        )
        .order_by(
            WellnessEvent.recorded_at.desc(),
            WellnessEvent.created_at.desc(),
        )
    )
    for row in rows:
        snapshot = _snapshot_from_observation(
            session,
            row,
            now=current,
        )
        if (
            snapshot is not None
            and snapshot.local_day == local_day
            and snapshot.timezone == timezone
            and snapshot.collected_at <= current + _MAX_CLOCK_SKEW
        ):
            return snapshot
    return None
