"""Continuous raw-first ingestion (docs/PLAN.md §13).

Owner decision 2026-07-16: meaningful data must keep accumulating **raw** —
long-horizon unstructured payloads become interpretable as models improve,
so acceptance never depends on today's parser. Three stages, strictly
ordered:

1. ``store_raw`` — the verbatim body is written under
   ``HEALTHMES_DATA_DIR/raw_ingest/YYYY/MM/DD/`` (owner-only files) and
   indexed in ``raw_ingest_event`` *before* anything tries to read it.
   This is the only stage that can fail the request.
2. ``transform_hae`` — best-effort mapping of a Health Auto Export-style
   payload (the de-facto contract of off-the-shelf HealthKit auto-export
   apps) to the open-wearables mobile-SDK sync contract
   (``vendor/open-wearables/.../schemas/providers/mobile_sdk/sync_request.py``).
   Only quantity metrics are mapped; sleep aggregates stay raw-only because
   daily sums cannot honestly be reconstructed into stage intervals.
3. ``forward_sdk_sync`` — POST to open-wearables
   ``/api/v1/sdk/users/{user_id}/sync`` so the data plane normalizes the
   mapped records like any phone-SDK push.

Failures in 2–3 are recorded on the index row and never surface as request
errors: the raw payload is already durable.
"""

from __future__ import annotations

import hashlib
import logging
import math
import uuid as uuid_module
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from healthmes.config import Settings
from healthmes.durable_files import (
    DurableFileIdentity,
    DurablePublishError,
    durable_exclusive_writer,
    durable_publish_no_clobber,
    durable_unlink,
    verify_regular_file,
    write_all,
)
from healthmes.source_providers import canonical_source_provider
from healthmes.store import RawIngestEvent

logger = logging.getLogger(__name__)

RAW_INGEST_DIRNAME = "raw_ingest"

# Health Auto Export metric name → (HealthKit identifier, value key).
# HK identifiers are the open-wearables SDK contract's native ``type`` values
# (SDKMetricType); only signals the cognitive-energy loop consumes are mapped
# — everything else still lands in the raw store.
HAE_METRIC_MAP: dict[str, str] = {
    "heart_rate": "HKQuantityTypeIdentifierHeartRate",
    "resting_heart_rate": "HKQuantityTypeIdentifierRestingHeartRate",
    "heart_rate_variability": "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
    "respiratory_rate": "HKQuantityTypeIdentifierRespiratoryRate",
    "blood_oxygen_saturation": "HKQuantityTypeIdentifierOxygenSaturation",
    "step_count": "HKQuantityTypeIdentifierStepCount",
    "active_energy": "HKQuantityTypeIdentifierActiveEnergyBurned",
    "apple_sleeping_wrist_temperature": (
        "HKQuantityTypeIdentifierAppleSleepingWristTemperature"
    ),
    "walking_running_distance": "HKQuantityTypeIdentifierDistanceWalkingRunning",
}

_HAE_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z")


class IngestForwardError(Exception):
    """open-wearables rejected or never received the forwarded batch."""


class InvalidHealthKitPayloadError(ValueError):
    """A recognized first-party HealthKit payload violated its wire contract."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True, slots=True)
class HealthKitNativeBatch:
    """Open Wearables-compatible arrays from ``healthmes.healthkit.v1``."""

    sdk_version: str
    sync_timestamp: str
    records: tuple[dict[str, Any], ...]
    sleep: tuple[dict[str, Any], ...]
    workouts: tuple[dict[str, Any], ...]
    deletions: tuple[tuple[str, str], ...]

    @property
    def candidate_ids(self) -> set[str]:
        return {
            sample_id
            for row in (*self.records, *self.sleep, *self.workouts)
            if (sample_id := _native_id(row)) is not None
        }

    def suppress(self, tombstoned_ids: set[str]) -> HealthKitNativeBatch:
        def retained(
            rows: tuple[dict[str, Any], ...],
        ) -> tuple[dict[str, Any], ...]:
            return tuple(
                row
                for row in rows
                if (sample_id := _native_id(row)) is None
                or sample_id not in tombstoned_ids
            )

        return HealthKitNativeBatch(
            sdk_version=self.sdk_version,
            sync_timestamp=self.sync_timestamp,
            records=retained(self.records),
            sleep=retained(self.sleep),
            workouts=retained(self.workouts),
            deletions=self.deletions,
        )


@dataclass(frozen=True, slots=True)
class RawIngestPublication:
    event: RawIngestEvent
    staged: Path
    destination: Path
    identity: DurableFileIdentity
    destination_durable: bool


def _utcnow() -> datetime:
    """Aware UTC — the column is TIMESTAMPTZ; naive values would take the
    postgres session timezone."""
    return datetime.now(UTC)


def _extension_for(content_type: str | None) -> str:
    lowered = (content_type or "").lower()
    if "json" in lowered:
        return ".json"
    if "xml" in lowered:
        return ".xml"
    return ".bin"


def store_raw(
    settings: Settings,
    *,
    source: str,
    content_type: str | None,
    body: bytes,
) -> RawIngestPublication:
    """Write ``body`` verbatim to the raw store and return the unsaved index row.

    The caller adds the row to its session; the file is on disk (0600,
    date-partitioned, content-hash suffixed so identical re-posts never
    collide with different payloads) before this returns.
    """
    source = canonical_source_provider(source)
    received = _utcnow()
    digest = hashlib.sha256(body).hexdigest()
    rel_dir = (
        f"{RAW_INGEST_DIRNAME}/{received:%Y}/{received:%m}/{received:%d}"
    )
    filename = f"{received:%H%M%S_%f}-{digest[:12]}{_extension_for(content_type)}"
    target = settings.data_dir / rel_dir / filename
    staged = (
        settings.data_dir
        / ".staging"
        / rel_dir
        / f"{filename}.part"
    )
    try:
        with durable_exclusive_writer(staged) as output:
            write_all(output, body)
    except BaseException:
        try:
            durable_unlink(staged, missing_ok=True)
        except OSError:
            logger.exception(
                "failed to durably remove incomplete raw-ingest staging file %s",
                staged,
            )
        raise
    destination_durable = True
    try:
        publication_identity = durable_publish_no_clobber(staged, target)
    except FileExistsError:
        try:
            durable_unlink(staged, missing_ok=True)
        except OSError:
            logger.exception(
                "failed to durably remove collided raw-ingest staging file %s",
                staged,
            )
        raise
    except DurablePublishError as exc:
        if not exc.destination_created:
            try:
                durable_unlink(staged, missing_ok=True)
            except OSError:
                logger.exception(
                    "failed to durably remove unpublished raw-ingest staging "
                    "file %s",
                    staged,
                )
            raise
        if exc.identity is None:
            raise
        publication_identity = exc.identity
        destination_durable = False
        verify_regular_file(
            staged,
            publication_identity,
            expected_size=len(body),
            expected_sha256=digest,
        )
        logger.warning(
            "raw-ingest destination durability could not be confirmed; "
            "continuing from the crash-durable staging generation"
        )

    verify_regular_file(
        target,
        publication_identity,
        expected_size=len(body),
        expected_sha256=digest,
    )
    return RawIngestPublication(
        event=RawIngestEvent(
            received_at=received,
            source=source,
            content_type=(content_type[:255] if content_type else None),
            path=f"{rel_dir}/{filename}",
            size_bytes=len(body),
            sha256=digest,
        ),
        staged=staged,
        destination=target,
        identity=publication_identity,
        destination_durable=destination_durable,
    )


def _parse_hae_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    for fmt in _HAE_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _point_value(point: dict[str, Any]) -> Any | None:
    """The numeric reading of one HAE data point.

    Most metrics use ``qty``; heart rate uses ``Min``/``Avg``/``Max`` — the
    average is the honest single representative.
    """
    for key in ("qty", "Avg", "avg"):
        value = point.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if math.isfinite(value):
                    return value
            except OverflowError:  # ints beyond float range are not plausible readings
                pass
    return None


def transform_hae(payload: Any) -> list[dict[str, Any]]:
    """Best-effort SDK ``records`` from a Health Auto Export-style payload.

    Unknown metric names, malformed points, and non-dict shapes are skipped
    silently — they remain available in the raw store. Returns ``[]`` when
    nothing mapped.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        return []

    records: list[dict[str, Any]] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        hk_type = HAE_METRIC_MAP.get(str(metric.get("name", "")).lower())
        if hk_type is None:
            continue
        unit = metric.get("units") if isinstance(metric.get("units"), str) else None
        points = metric.get("data")
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict):
                continue
            start = _parse_hae_date(point.get("date"))
            value = _point_value(point)
            if start is None or value is None:
                continue
            end = _parse_hae_date(point.get("endDate")) or start
            records.append(
                {
                    "type": hk_type,
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "value": value,
                    "unit": unit,
                }
            )
    return records


def _native_rows(
    value: Any,
    path: str,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise InvalidHealthKitPayloadError(
            path,
            "must be an array",
        )
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise InvalidHealthKitPayloadError(
                f"{path}[{index}]",
                "each array item must be an object",
            )
        rows.append(dict(row))
    return tuple(rows)


def _native_id(row: dict[str, Any]) -> str | None:
    value = row.get("id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized else None


def is_healthkit_v1_payload(payload: Any) -> bool:
    """Return whether ``payload`` claims the first-party HealthKit schema."""

    return (
        isinstance(payload, dict)
        and payload.get("schema") == "healthmes.healthkit.v1"
    )


def _native_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidHealthKitPayloadError(path, "must be a non-empty string")
    return value.strip()


def _native_timestamp(value: Any, path: str) -> datetime:
    raw = _native_string(value, path)
    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
    except ValueError as exc:
        raise InvalidHealthKitPayloadError(
            path,
            "must be an ISO 8601 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidHealthKitPayloadError(
            path,
            "must include a UTC offset",
        )
    return parsed


def _native_number(value: Any, path: str) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidHealthKitPayloadError(
            path,
            "must be a finite number",
        )
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise InvalidHealthKitPayloadError(path, "must be a finite number")
    return value


def _validate_native_interval(row: dict[str, Any], path: str) -> None:
    start = _native_timestamp(row.get("startDate"), f"{path}.startDate")
    end = _native_timestamp(row.get("endDate"), f"{path}.endDate")
    if end < start:
        raise InvalidHealthKitPayloadError(
            f"{path}.endDate",
            "must not be earlier than startDate",
        )


def _validate_native_optional_metadata(
    row: dict[str, Any],
    path: str,
) -> None:
    if "zoneOffset" in row and row["zoneOffset"] is not None:
        _native_string(row["zoneOffset"], f"{path}.zoneOffset")
    if "source" in row and row["source"] is not None:
        if not isinstance(row["source"], dict):
            raise InvalidHealthKitPayloadError(
                f"{path}.source",
                "must be an object when present",
            )


def _validate_native_metric(row: dict[str, Any], path: str) -> None:
    _native_string(row.get("id"), f"{path}.id")
    _native_string(row.get("type"), f"{path}.type")
    _validate_native_interval(row, path)
    _native_number(row.get("value"), f"{path}.value")
    _native_string(row.get("unit"), f"{path}.unit")
    _validate_native_optional_metadata(row, path)


def _validate_native_sleep(row: dict[str, Any], path: str) -> None:
    _native_string(row.get("id"), f"{path}.id")
    _native_string(row.get("stage"), f"{path}.stage")
    _validate_native_interval(row, path)
    _validate_native_optional_metadata(row, path)


def _validate_native_workout(row: dict[str, Any], path: str) -> None:
    _native_string(row.get("id"), f"{path}.id")
    _native_string(row.get("type"), f"{path}.type")
    _validate_native_interval(row, path)
    values = row.get("values")
    if not isinstance(values, list):
        raise InvalidHealthKitPayloadError(
            f"{path}.values",
            "must be an array",
        )
    for index, statistic in enumerate(values):
        statistic_path = f"{path}.values[{index}]"
        if not isinstance(statistic, dict):
            raise InvalidHealthKitPayloadError(
                statistic_path,
                "must be an object",
            )
        _native_string(statistic.get("type"), f"{statistic_path}.type")
        _native_string(statistic.get("unit"), f"{statistic_path}.unit")
        _native_number(statistic.get("value"), f"{statistic_path}.value")
    _validate_native_optional_metadata(row, path)


def _validate_native_deletion(
    row: dict[str, Any],
    path: str,
) -> tuple[str, str]:
    return (
        _native_string(row.get("id"), f"{path}.id"),
        _native_string(row.get("type"), f"{path}.type"),
    )


def transform_healthkit_v1(payload: Any) -> HealthKitNativeBatch | None:
    """Strictly validate and preserve the first-party HealthKit wire schema."""

    if not is_healthkit_v1_payload(payload):
        return None
    assert isinstance(payload, dict)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidHealthKitPayloadError("data", "must be an object")

    records = _native_rows(data.get("records"), "data.records")
    sleep = _native_rows(data.get("sleep"), "data.sleep")
    workouts = _native_rows(data.get("workouts"), "data.workouts")
    deletion_rows = _native_rows(
        data.get("deletions"),
        "data.deletions",
    )

    for index, row in enumerate(records):
        _validate_native_metric(row, f"data.records[{index}]")
    for index, row in enumerate(sleep):
        _validate_native_sleep(row, f"data.sleep[{index}]")
    for index, row in enumerate(workouts):
        _validate_native_workout(row, f"data.workouts[{index}]")
    deletions = tuple(
        _validate_native_deletion(row, f"data.deletions[{index}]")
        for index, row in enumerate(deletion_rows)
    )

    sdk_version = _native_string(payload.get("sdkVersion"), "sdkVersion")
    sync_timestamp = _native_string(
        payload.get("syncTimestamp"),
        "syncTimestamp",
    )
    _native_timestamp(sync_timestamp, "syncTimestamp")

    return HealthKitNativeBatch(
        sdk_version=sdk_version,
        sync_timestamp=sync_timestamp,
        records=records,
        sleep=sleep,
        workouts=workouts,
        deletions=deletions,
    )


def forward_sdk_sync(
    settings: Settings,
    records: list[dict[str, Any]],
    *,
    user_id: str,
    sleep: list[dict[str, Any]] | None = None,
    workouts: list[dict[str, Any]] | None = None,
    sdk_version: str = "healthmes-bridge/1",
    sync_timestamp: str | None = None,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """POST mapped records to open-wearables' mobile-SDK sync endpoint."""
    api_key = settings.ow_api_key.get_secret_value()
    if not api_key:
        raise IngestForwardError("open-wearables API key missing (HEALTHMES_OW_API_KEY)")
    try:
        uuid_module.UUID(user_id)
    except ValueError as exc:
        # The vendor queues unknown users and its worker silently discards
        # them — a non-UUID id would be a false "queued" forever.
        raise IngestForwardError(
            f"HEALTHMES_OW_USER_ID must be the open-wearables user UUID, got {user_id!r}"
        ) from exc

    body = {
        "provider": "apple",
        "sdkVersion": sdk_version,
        "syncTimestamp": sync_timestamp or datetime.now(UTC).isoformat(),
        "data": {
            "records": records,
            "sleep": sleep or [],
            "workouts": workouts or [],
        },
    }
    url = f"{settings.ow_base_url.rstrip('/')}/api/v1/sdk/users/{user_id}/sync"
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(
                url, json=body, headers={"X-Open-Wearables-API-Key": api_key}
            )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # HTTPError: transport; ValueError/TypeError: body not JSON-encodable
        # (e.g. non-finite floats). Never repr the request — headers carry
        # the API key.
        raise IngestForwardError(f"{exc.__class__.__name__}: {exc}") from exc
    if response.status_code != 202:
        # The vendor contract is an explicit 202 queue ack; anything else
        # (including 3xx from a proxy) is not an acceptance.
        detail = response.text.replace(api_key, "***")[:200]
        raise IngestForwardError(f"HTTP {response.status_code} — {detail}")
