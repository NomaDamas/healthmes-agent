"""Contract tests: the batch example documented in apps/android-usage/README.md
round-trips through the real ``POST /v1/app-usage/batch`` endpoint.

The Android collector cannot be compiled/exercised in this test suite, so the
README's payload example is the pinned wire contract: these tests parse the
exact fenced JSON blocks out of the README (located by HTML-comment markers)
and replay them against the app. If the endpoint schema drifts, this fails and
the README + collector must follow.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from healthmes.store import AppUsageSample

README_PATH = Path(__file__).resolve().parents[2] / "apps" / "android-usage" / "README.md"

PAYLOAD_MARKER = "ingest-payload-example"
ACK_MARKER = "ingest-ack-example"


def _documented_json(marker: str) -> dict:
    """Parse the fenced ```json block that directly follows an HTML marker."""
    text = README_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"<!-- {re.escape(marker)} -->\s*```json\n(.*?)```",
        text,
        flags=re.DOTALL,
    )
    assert match is not None, f"marker <!-- {marker} --> with a ```json block not in {README_PATH}"
    return json.loads(match.group(1))


def _as_utc(value: datetime) -> datetime:
    """sqlite returns naive datetimes; by API contract they are UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@pytest.fixture
def payload() -> dict:
    return _documented_json(PAYLOAD_MARKER)


def test_readme_payload_round_trips_through_ingest(client, session, payload):
    documented_ack = _documented_json(ACK_MARKER)

    response = client.post("/v1/app-usage/batch", json=payload)

    assert response.status_code == 200
    assert response.json() == documented_ack

    rows = session.scalars(select(AppUsageSample)).all()
    documented = {
        (_as_utc(datetime.fromisoformat(sample["bucket_start"])), sample["app_package"]): sample
        for sample in payload["samples"]
    }
    assert len(rows) == len(documented) == documented_ack["accepted"]
    for row in rows:
        sample = documented[(_as_utc(row.bucket_start), row.app_package)]
        assert row.device_id == payload["device_id"]
        assert row.foreground_seconds == sample["foreground_seconds"]
        assert row.launches == sample["launches"]
        assert row.category == sample["category"]


def test_readme_payload_reupload_is_idempotent_upsert(client, session, payload):
    """The collector re-sends the growing hour every run; rows must not pile up."""
    first = client.post("/v1/app-usage/batch", json=payload)
    second = client.post("/v1/app-usage/batch", json=payload)

    assert first.status_code == second.status_code == 200
    accepted = first.json()["accepted"]
    assert second.json() == {"accepted": accepted, "created": 0, "updated": accepted}
    assert len(session.scalars(select(AppUsageSample)).all()) == accepted


def test_readme_payload_matches_collector_invariants(payload):
    """The documented example must reflect what the Android bucketer emits."""
    assert 1 <= len(payload["samples"]) <= 1000
    assert 1 <= len(payload["device_id"]) <= 64
    assert payload["device_id"].startswith("android-")
    for sample in payload["samples"]:
        # Top-of-hour UTC instants with a Z suffix (java.time.Instant.toString()).
        assert sample["bucket_start"].endswith("Z")
        parsed = datetime.fromisoformat(sample["bucket_start"])
        assert (parsed.minute, parsed.second, parsed.microsecond) == (0, 0, 0)
        # HourlyBucketer clamps one app's seconds to one bucket length.
        assert 0 <= sample["foreground_seconds"] <= 3600
        assert sample["launches"] >= 0
        assert sample["category"] is None or 1 <= len(sample["category"]) <= 64
