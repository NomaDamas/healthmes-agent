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

import hashlib
import logging
import math
import uuid as uuid_module
from datetime import UTC, datetime
from typing import Any

import httpx

from healthmes.config import Settings
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


def _utcnow_naive() -> datetime:
    """Store convention: naive UTC (matches every other timestamp column)."""
    return datetime.now(UTC).replace(tzinfo=None)


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
) -> RawIngestEvent:
    """Write ``body`` verbatim to the raw store and return the unsaved index row.

    The caller adds the row to its session; the file is on disk (0600,
    date-partitioned, content-hash suffixed so identical re-posts never
    collide with different payloads) before this returns.
    """
    received = _utcnow_naive()
    digest = hashlib.sha256(body).hexdigest()
    rel_dir = (
        f"{RAW_INGEST_DIRNAME}/{received:%Y}/{received:%m}/{received:%d}"
    )
    filename = f"{received:%H%M%S_%f}-{digest[:12]}{_extension_for(content_type)}"
    target_dir = settings.data_dir / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    target.write_bytes(body)
    target.chmod(0o600)

    return RawIngestEvent(
        received_at=received,
        source=source,
        content_type=(content_type[:255] if content_type else None),
        path=f"{rel_dir}/{filename}",
        size_bytes=len(body),
        sha256=digest,
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
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return value
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


def forward_sdk_sync(
    settings: Settings,
    records: list[dict[str, Any]],
    *,
    user_id: str,
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
        "sdkVersion": "healthmes-bridge/1",
        "syncTimestamp": datetime.now(UTC).isoformat(),
        "data": {"records": records, "sleep": [], "workouts": []},
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
