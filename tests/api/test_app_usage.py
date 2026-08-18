"""Tests for the Android app-usage batch ingest endpoint."""

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from healthmes.activity.android import ANDROID_BUCKET_SNAPSHOT_EVENT
from healthmes.activity.contracts import ActivityCollectionStatusUpdate
from healthmes.activity.maintenance import run_activity_maintenance
from healthmes.activity.repository import (
    APP_HOUR_EVENT,
    DAY_SUMMARY_EVENT,
    update_collection_status,
)
from healthmes.storage import update_retention_policy
from healthmes.store import AppUsageSample, WellnessEvent


def _batch(samples):
    return {
        "device_id": "pixel-8-test",
        "collection_revision": 0,
        "collection_generation": 0,
        "pairing_revision": 0,
        "samples": samples,
    }


def _snapshot_batch(
    samples,
    *,
    sequence: int,
    bucket_complete: bool,
    bucket_starts=None,
):
    normalized = [
        {
            **sample,
            "bucket_complete": bucket_complete,
            "snapshot_sequence": sequence,
        }
        for sample in samples
    ]
    starts = bucket_starts or sorted(
        {sample["bucket_start"] for sample in normalized}
    )
    return {
        **_batch(normalized),
        "bucket_snapshots": [
            {
                "bucket_start": bucket_start,
                "bucket_complete": bucket_complete,
                "snapshot_sequence": sequence,
                "app_packages": sorted(
                    sample["app_package"]
                    for sample in normalized
                    if sample["bucket_start"] == bucket_start
                ),
            }
            for bucket_start in starts
        ],
    }


def _set_generation(
    client,
    *,
    generation: int,
    observed_at: str,
    device_id: str = "pixel-8-test",
    granted: bool = True,
    pairing_revision: int = 0,
):
    return client.post(
        f"/v1/activity/devices/{device_id}/status",
        json={
            "platform": "android",
            "capability": "aggregate",
            "permission_status": "granted" if granted else "revoked",
            "status_reason": None if granted else "usage_access_revoked",
            "status_observed_at": observed_at,
            "collection_generation": generation,
            "pairing_revision": pairing_revision,
            "queue_depth": 0,
        },
    )


@pytest.fixture(autouse=True)
def register_default_generation(client):
    # Build FastAPI before freezing datetime to avoid lazy schema compilation
    # seeing freezegun's date subclass.
    assert client.get("/v1/activity/devices/clock-prime/collection").status_code == 200
    with freeze_time("2026-08-14 12:00:00", tick=True, real_asyncio=True):
        response = _set_generation(
            client,
            generation=0,
            observed_at="2026-08-01T09:00:00Z",
        )
        assert response.status_code == 200
        yield


SAMPLE_SLACK = {
    "bucket_start": "2026-08-01T10:00:00Z",
    "app_package": "com.slack",
    "foreground_seconds": 340,
    "launches": 9,
    "category": "communication",
    "bucket_complete": True,
}
SAMPLE_MAPS = {
    "bucket_start": "2026-08-01T10:00:00Z",
    "app_package": "com.google.maps",
    "foreground_seconds": 120,
    "launches": 2,
    "bucket_complete": True,
}


def test_batch_ingest_creates_rows(client, session):
    response = client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK, SAMPLE_MAPS]))

    assert response.status_code == 200
    assert response.json() == {
        "accepted": 2,
        "created": 2,
        "updated": 0,
        "suppressed": 0,
    }

    rows = session.scalars(select(AppUsageSample)).all()
    assert {row.app_package for row in rows} == {"com.slack", "com.google.maps"}
    slack = next(row for row in rows if row.app_package == "com.slack")
    assert slack.device_id == "pixel-8-test"
    assert slack.foreground_seconds == 340
    assert slack.launches == 9
    assert slack.category == "communication"
    canonical = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    ).all()
    assert {row.payload["app_id"] for row in canonical} == {
        "com.slack",
        "com.google.maps",
    }


def test_legacy_payload_without_completion_is_provisional(client, session):
    legacy = {
        key: value
        for key, value in SAMPLE_SLACK.items()
        if key != "bucket_complete"
    }

    response = client.post("/v1/app-usage/batch", json=_batch([legacy]))

    assert response.status_code == 200
    row = session.scalar(select(AppUsageSample))
    canonical = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    )
    assert row is not None
    assert row.bucket_complete is False
    assert canonical is not None
    assert canonical.payload["bucket_complete"] is False


def test_batch_ingest_upserts_growing_bucket(client, session):
    provisional = {**SAMPLE_SLACK, "bucket_complete": False}
    client.post("/v1/app-usage/batch", json=_batch([provisional]))

    grown = {
        **provisional,
        "foreground_seconds": 900,
        "launches": 15,
    }
    response = client.post("/v1/app-usage/batch", json=_batch([grown]))

    assert response.json() == {
        "accepted": 1,
        "created": 0,
        "updated": 1,
        "suppressed": 0,
    }
    rows = session.scalars(select(AppUsageSample)).all()
    assert len(rows) == 1
    assert rows[0].foreground_seconds == 900
    assert rows[0].launches == 15
    assert rows[0].bucket_complete is False
    canonical = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    )
    assert canonical is not None
    assert canonical.payload["foreground_seconds"] == 900
    assert canonical.payload["bucket_complete"] is False


def test_batch_ingest_finalizes_provisional_bucket_once(client, session):
    provisional = {
        **SAMPLE_SLACK,
        "foreground_seconds": 600,
        "launches": 10,
        "bucket_complete": False,
    }
    finalized = {
        **provisional,
        "foreground_seconds": 900,
        "launches": 15,
        "bucket_complete": True,
    }

    first = client.post("/v1/app-usage/batch", json=_batch([provisional]))
    second = client.post("/v1/app-usage/batch", json=_batch([finalized]))
    duplicate = client.post("/v1/app-usage/batch", json=_batch([finalized]))

    assert first.status_code == 200
    assert second.json() == {
        "accepted": 1,
        "created": 0,
        "updated": 1,
        "suppressed": 0,
    }
    assert duplicate.json() == {
        "accepted": 1,
        "created": 0,
        "updated": 0,
        "suppressed": 0,
    }
    row = session.scalar(select(AppUsageSample))
    canonical = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    )
    assert row is not None
    assert row.bucket_complete is True
    assert canonical is not None
    assert canonical.payload["bucket_complete"] is True


@pytest.mark.parametrize(
    "replacement",
    (
        {
            **SAMPLE_SLACK,
            "foreground_seconds": 339,
            "bucket_complete": False,
        },
        {
            **SAMPLE_SLACK,
            "foreground_seconds": 900,
            "bucket_complete": False,
        },
        {
            **SAMPLE_SLACK,
            "foreground_seconds": 900,
            "bucket_complete": True,
        },
    ),
    ids=("reopen-lower", "reopen-higher", "rewrite-complete"),
)
def test_batch_ingest_rejects_changes_after_bucket_completion(
    client,
    session,
    replacement,
):
    created = client.post(
        "/v1/app-usage/batch",
        json=_batch([SAMPLE_SLACK]),
    )
    response = client.post(
        "/v1/app-usage/batch",
        json=_batch([replacement]),
    )

    assert created.status_code == 200
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_source_conflict"
    row = session.scalar(select(AppUsageSample))
    assert row is not None
    assert row.foreground_seconds == SAMPLE_SLACK["foreground_seconds"]
    assert row.bucket_complete is True


def test_batch_ingest_rejects_provisional_value_decrease(client, session):
    initial = {
        **SAMPLE_SLACK,
        "foreground_seconds": 900,
        "launches": 15,
        "bucket_complete": False,
    }
    decreased = {
        **initial,
        "foreground_seconds": 899,
    }

    created = client.post("/v1/app-usage/batch", json=_batch([initial]))
    response = client.post("/v1/app-usage/batch", json=_batch([decreased]))

    assert created.status_code == 200
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_source_conflict"
    row = session.scalar(select(AppUsageSample))
    assert row is not None
    assert row.foreground_seconds == 900
    assert row.bucket_complete is False


def test_newer_snapshot_sequence_can_authoritatively_correct_provisional(
    client,
    session,
):
    initial = {
        **SAMPLE_SLACK,
        "foreground_seconds": 900,
        "launches": 15,
        "bucket_complete": False,
        "snapshot_sequence": 100,
    }
    corrected = {
        **initial,
        "foreground_seconds": 850,
        "launches": 14,
        "snapshot_sequence": 101,
    }

    created = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [initial],
            sequence=100,
            bucket_complete=False,
        ),
    )
    response = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [corrected],
            sequence=101,
            bucket_complete=False,
        ),
    )

    assert created.status_code == 200
    assert response.status_code == 200
    row = session.scalar(select(AppUsageSample))
    canonical = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    )
    assert row is not None
    assert row.foreground_seconds == 850
    assert row.launches == 14
    assert row.snapshot_sequence == 101
    assert canonical is not None
    assert canonical.payload["snapshot_sequence"] == 101
    assert canonical.payload["foreground_seconds"] == 850


@pytest.mark.parametrize("stale_sequence", (99, 100))
def test_stale_or_equal_snapshot_sequence_cannot_change_provisional(
    client,
    session,
    stale_sequence,
):
    initial = {
        **SAMPLE_SLACK,
        "foreground_seconds": 900,
        "bucket_complete": False,
        "snapshot_sequence": 100,
    }
    stale = {
        **initial,
        "foreground_seconds": 901,
        "snapshot_sequence": stale_sequence,
    }

    created = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [initial],
            sequence=100,
            bucket_complete=False,
        ),
    )
    response = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [stale],
            sequence=stale_sequence,
            bucket_complete=False,
        ),
    )

    assert created.status_code == 200
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_source_conflict"
    row = session.scalar(select(AppUsageSample))
    assert row is not None
    assert row.foreground_seconds == 900
    assert row.snapshot_sequence == 100


def test_later_identical_completed_snapshot_is_idempotent(client, session):
    completed = {
        **SAMPLE_SLACK,
        "snapshot_sequence": 100,
    }
    replay = {
        **completed,
        "snapshot_sequence": 101,
    }

    created = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [completed],
            sequence=100,
            bucket_complete=True,
        ),
    )
    duplicate = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [replay],
            sequence=101,
            bucket_complete=True,
        ),
    )

    assert created.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["updated"] == 0
    row = session.scalar(select(AppUsageSample))
    assert row is not None
    assert row.bucket_complete is True
    assert row.snapshot_sequence == 100


def test_newer_authoritative_snapshot_removes_missing_package(
    client,
    session,
):
    first = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_SLACK, SAMPLE_MAPS],
            sequence=100,
            bucket_complete=False,
        ),
    )
    second = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_SLACK],
            sequence=101,
            bucket_complete=False,
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert {
        row.app_package
        for row in session.scalars(select(AppUsageSample))
    } == {"com.slack"}
    assert {
        row.payload["app_id"]
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    } == {"com.slack"}


def test_stale_authoritative_snapshot_cannot_reinsert_missing_package(
    client,
    session,
):
    initial = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_SLACK, SAMPLE_MAPS],
            sequence=100,
            bucket_complete=False,
        ),
    )
    newest = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_SLACK],
            sequence=101,
            bucket_complete=False,
        ),
    )
    stale = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_MAPS],
            sequence=100,
            bucket_complete=False,
        ),
    )

    assert initial.status_code == 200
    assert newest.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "activity_source_conflict"
    assert {
        row.app_package
        for row in session.scalars(select(AppUsageSample))
    } == {"com.slack"}


def test_newer_empty_authoritative_snapshot_deletes_hour(client, session):
    created = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_SLACK, SAMPLE_MAPS],
            sequence=100,
            bucket_complete=False,
        ),
    )
    emptied = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [],
            sequence=101,
            bucket_complete=False,
            bucket_starts=[SAMPLE_SLACK["bucket_start"]],
        ),
    )

    assert created.status_code == 200
    assert emptied.status_code == 200
    assert emptied.json() == {
        "accepted": 0,
        "created": 0,
        "updated": 0,
        "suppressed": 0,
    }
    assert list(session.scalars(select(AppUsageSample))) == []
    assert list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    ) == []


def test_incomplete_empty_source_set_preserves_completed_hour_and_new_upload(
    client,
    session,
):
    completed = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_SLACK],
            sequence=100,
            bucket_complete=True,
        ),
    )
    next_hour = {
        **SAMPLE_MAPS,
        "bucket_start": "2026-08-01T11:00:00Z",
    }
    payload = _snapshot_batch(
        [next_hour],
        sequence=101,
        bucket_complete=True,
    )
    payload["bucket_snapshots"].insert(
        0,
        {
            "bucket_start": SAMPLE_SLACK["bucket_start"],
            "bucket_complete": True,
            "snapshot_sequence": 101,
            "source_set_complete": False,
            "app_packages": [],
        },
    )

    uploaded = client.post("/v1/app-usage/batch", json=payload)

    assert completed.status_code == 200
    assert uploaded.status_code == 200
    assert uploaded.json()["accepted"] == 1
    rows = list(
        session.scalars(
            select(AppUsageSample).order_by(
                AppUsageSample.bucket_start
            )
        )
    )
    assert [
        (
            row.bucket_start.isoformat(),
            row.app_package,
            row.snapshot_sequence,
        )
        for row in rows
    ] == [
        ("2026-08-01T10:00:00", "com.slack", 100),
        ("2026-08-01T11:00:00", "com.google.maps", 101),
    ]


def test_expired_incomplete_heartbeat_does_not_block_current_snapshot(
    client,
    session,
):
    now = datetime.now(UTC)
    update_retention_policy(
        session,
        "activity_raw",
        "1d",
        now=now,
    )
    session.commit()
    expired_bucket = (now - timedelta(days=2)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    current_bucket = now.replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    current_sample = {
        **SAMPLE_MAPS,
        "bucket_start": current_bucket.isoformat(),
    }
    payload = _snapshot_batch(
        [current_sample],
        sequence=100,
        bucket_complete=False,
    )
    payload["bucket_snapshots"].insert(
        0,
        {
            "bucket_start": expired_bucket.isoformat(),
            "bucket_complete": True,
            "snapshot_sequence": 100,
            "source_set_complete": False,
            "app_packages": [],
        },
    )

    response = client.post("/v1/app-usage/batch", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "accepted": 1,
        "created": 1,
        "updated": 0,
        "suppressed": 0,
    }
    rows = list(session.scalars(select(AppUsageSample)))
    assert len(rows) == 1
    assert rows[0].bucket_start == current_bucket.replace(tzinfo=None)
    assert rows[0].app_package == "com.google.maps"
    manifests = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type
                == ANDROID_BUCKET_SNAPSHOT_EVENT
            )
        )
    )
    assert len(manifests) == 1
    assert manifests[0].observed_at == current_bucket.replace(tzinfo=None)


def test_empty_authoritative_snapshot_outside_retention_is_rejected(
    client,
    session,
):
    now = datetime.now(UTC)
    update_retention_policy(
        session,
        "activity_raw",
        "1d",
        now=now,
    )
    session.commit()
    expired_bucket = (now - timedelta(days=2)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    response = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [],
            sequence=100,
            bucket_complete=True,
            bucket_starts=[expired_bucket.isoformat()],
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_outside_retention"
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type
                == ANDROID_BUCKET_SNAPSHOT_EVENT
            )
        )
        is None
    )


def test_future_empty_authoritative_snapshot_is_rejected_without_fence(
    client,
    session,
):
    future_bucket = (datetime.now(UTC) + timedelta(hours=2)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    response = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [],
            sequence=100,
            bucket_complete=True,
            bucket_starts=[future_bucket.isoformat()],
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_future_data"
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type
                == ANDROID_BUCKET_SNAPSHOT_EVENT
            )
        )
        is None
    )


def test_activity_maintenance_expires_android_snapshot_state(
    client,
    session,
):
    bucket_start = (datetime.now(UTC) - timedelta(hours=2)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    sample = {
        **SAMPLE_SLACK,
        "bucket_start": bucket_start.isoformat(),
    }
    created = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [sample],
            sequence=100,
            bucket_complete=True,
        ),
    )
    assert created.status_code == 200
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type
                == ANDROID_BUCKET_SNAPSHOT_EVENT
            )
        )
        is not None
    )

    run_activity_maintenance(
        session,
        now=bucket_start + timedelta(days=15),
    )

    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type
                == ANDROID_BUCKET_SNAPSHOT_EVENT
            )
        )
        is None
    )


def test_authoritative_batch_conflict_rolls_back_other_hours(
    client,
    session,
):
    second_hour = {
        **SAMPLE_MAPS,
        "bucket_start": "2026-08-01T11:00:00Z",
    }
    seeded = client.post(
        "/v1/app-usage/batch",
        json=_snapshot_batch(
            [SAMPLE_SLACK, second_hour],
            sequence=100,
            bucket_complete=False,
        ),
    )
    replacement = _snapshot_batch(
        [],
        sequence=101,
        bucket_complete=False,
        bucket_starts=[
            SAMPLE_SLACK["bucket_start"],
            second_hour["bucket_start"],
        ],
    )
    replacement["bucket_snapshots"][1]["snapshot_sequence"] = 99

    rejected = client.post("/v1/app-usage/batch", json=replacement)

    assert seeded.status_code == 200
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "activity_source_conflict"
    assert {
        (row.bucket_start.isoformat(), row.app_package)
        for row in session.scalars(select(AppUsageSample))
    } == {
        ("2026-08-01T10:00:00", "com.slack"),
        ("2026-08-01T11:00:00", "com.google.maps"),
    }


def test_authoritative_manifest_must_match_sample_rows(client, session):
    payload = _snapshot_batch(
        [SAMPLE_SLACK],
        sequence=100,
        bucket_complete=False,
    )
    payload["bucket_snapshots"][0]["app_packages"] = ["com.google.maps"]

    response = client.post("/v1/app-usage/batch", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert session.scalar(select(AppUsageSample)) is None


def test_incomplete_source_set_cannot_carry_samples(client, session):
    payload = _snapshot_batch(
        [SAMPLE_SLACK],
        sequence=100,
        bucket_complete=False,
    )
    payload["bucket_snapshots"][0]["source_set_complete"] = False

    response = client.post("/v1/app-usage/batch", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert session.scalar(select(AppUsageSample)) is None


def test_batch_ingest_dedupes_exact_replays_within_payload(client, session):
    sample = {**SAMPLE_SLACK, "foreground_seconds": 250}

    response = client.post("/v1/app-usage/batch", json=_batch([sample, sample]))

    assert response.json() == {
        "accepted": 1,
        "created": 1,
        "updated": 0,
        "suppressed": 0,
    }
    rows = session.scalars(select(AppUsageSample)).all()
    assert len(rows) == 1
    assert rows[0].foreground_seconds == 250


def test_batch_ingest_rejects_conflicting_duplicates_within_payload(
    client,
    session,
):
    first = {**SAMPLE_SLACK, "foreground_seconds": 100}
    conflicting = {
        **SAMPLE_SLACK,
        "foreground_seconds": 250,
    }

    response = client.post(
        "/v1/app-usage/batch",
        json=_batch([first, conflicting]),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_source_conflict"
    assert session.scalar(select(AppUsageSample)) is None
    assert session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    ) is None


def test_batch_ingest_same_bucket_different_devices_kept_apart(client, session):
    client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))
    registered = _set_generation(
        client,
        device_id="tab-s9-test",
        generation=0,
        observed_at="2026-08-01T09:00:00Z",
    )
    assert registered.status_code == 200
    response = client.post(
        "/v1/app-usage/batch",
        json={
            "device_id": "tab-s9-test",
            "collection_revision": 0,
            "collection_generation": 0,
            "samples": [SAMPLE_SLACK],
        },
    )

    assert response.json() == {
        "accepted": 1,
        "created": 1,
        "updated": 0,
        "suppressed": 0,
    }
    assert len(session.scalars(select(AppUsageSample)).all()) == 2


def test_legacy_overflow_is_clamped_in_canonical_summary(client, session):
    overflow = {**SAMPLE_SLACK, "foreground_seconds": 7200}

    response = client.post("/v1/app-usage/batch", json=_batch([overflow]))

    assert response.status_code == 200
    legacy = session.scalar(select(AppUsageSample))
    summary = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert legacy is not None
    assert legacy.foreground_seconds == 7200
    assert summary is not None
    assert summary.payload["total_active_minutes"] == 60.0
    assert "source_reported_seconds_exceeded_bucket" in summary.payload["limitations"]


def test_batch_ingest_validation_errors(client):
    empty = client.post("/v1/app-usage/batch", json=_batch([]))
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "validation_error"

    negative = client.post(
        "/v1/app-usage/batch",
        json=_batch([{**SAMPLE_SLACK, "foreground_seconds": -1}]),
    )
    assert negative.status_code == 422

    no_device = client.post("/v1/app-usage/batch", json={"samples": [SAMPLE_SLACK]})
    assert no_device.status_code == 422


def test_batch_ingest_requires_collection_revision(client):
    response = client.post(
        "/v1/app-usage/batch",
        json={
            "device_id": "pixel-8-test",
            "samples": [SAMPLE_SLACK],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_batch_ingest_requires_collection_generation(client):
    payload = _batch([SAMPLE_SLACK])
    payload.pop("collection_generation")

    response = client.post("/v1/app-usage/batch", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_same_hour_privacy_generations_are_preserved(client, session):
    before_boundary = {
        **SAMPLE_SLACK,
        "foreground_seconds": 40 * 60,
        "launches": 4,
    }
    after_boundary = {
        **SAMPLE_SLACK,
        "foreground_seconds": 10 * 60,
        "launches": 1,
    }

    first_status = _set_generation(
        client,
        generation=7,
        observed_at="2026-08-01T09:30:00Z",
    )
    first = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([before_boundary]),
            "collection_generation": 7,
        },
    )
    second_status = _set_generation(
        client,
        generation=8,
        observed_at="2026-08-01T09:45:00Z",
    )
    second = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([after_boundary]),
            "collection_generation": 8,
        },
    )

    assert first_status.status_code == 200
    assert second_status.status_code == 200
    assert first.status_code == 200
    assert second.status_code == 200
    rows = list(
        session.scalars(
            select(AppUsageSample).order_by(
                AppUsageSample.collection_generation
            )
        )
    )
    assert [
        (row.collection_generation, row.foreground_seconds)
        for row in rows
    ] == [(7, 2400), (8, 600)]
    canonical = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )
    assert len(canonical) == 2
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert daily is not None
    assert daily.payload["total_active_minutes"] == 50.0


def test_timezone_change_keeps_collection_windows_in_separate_summaries(
    client,
    session,
):
    first_status = _set_generation(
        client,
        generation=9,
        observed_at="2026-08-01T09:30:00Z",
    )
    first = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch(
                [
                    {
                        **SAMPLE_SLACK,
                        "foreground_seconds": 30 * 60,
                    }
                ]
            ),
            "timezone": "Asia/Tokyo",
            "collection_generation": 9,
        },
    )
    second_status = _set_generation(
        client,
        generation=10,
        observed_at="2026-08-01T09:45:00Z",
    )
    second = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch(
                [
                    {
                        **SAMPLE_SLACK,
                        "foreground_seconds": 10 * 60,
                    }
                ]
            ),
            "timezone": "Asia/Seoul",
            "collection_generation": 10,
        },
    )

    assert first_status.status_code == 200
    assert second_status.status_code == 200
    assert first.status_code == 200
    assert second.status_code == 200
    raw = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )
    assert {row.timezone for row in raw} == {
        "Asia/Tokyo",
        "Asia/Seoul",
    }
    summaries = {
        row.timezone: row.payload["total_active_minutes"]
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT
            )
        )
    }
    assert summaries == {
        "Asia/Tokyo": 30.0,
        "Asia/Seoul": 10.0,
    }


def test_batch_ingest_rejects_future_sample_without_persisting(client, session):
    future_bucket = (datetime.now(UTC) + timedelta(hours=2)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    response = client.post(
        "/v1/app-usage/batch",
        json=_batch(
            [
                {
                    **SAMPLE_SLACK,
                    "bucket_start": future_bucket.isoformat(),
                }
            ]
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_future_data"
    assert session.scalar(select(AppUsageSample)) is None
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
        is None
    )


def test_deleted_legacy_sample_is_suppressed_on_retry(client, session):
    first = client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))
    assert first.status_code == 200
    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": "pixel-8-test",
            "start": "2026-08-01T10:00:00Z",
            "end": "2026-08-01T11:00:00Z",
            "include_summaries": True,
            "include_control": False,
            "confirm": True,
        },
    )
    assert deleted.status_code == 200

    retried = client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))

    assert retried.status_code == 200
    assert retried.json() == {
        "accepted": 0,
        "created": 0,
        "updated": 0,
        "suppressed": 1,
    }
    assert session.scalar(select(AppUsageSample)) is None


def test_batch_requires_registered_current_generation(client, session):
    response = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([SAMPLE_SLACK]),
            "device_id": "unregistered-android",
            "collection_generation": 4,
        },
    )

    assert response.status_code == 409
    assert (
        response.json()["error"]["code"]
        == "activity_collection_generation_unregistered"
    )
    assert session.scalar(select(AppUsageSample)) is None


@pytest.mark.parametrize(
    ("device_id", "status_payload"),
    (
        (
            "android-missing-boundary",
            {
                "collection_generation": 4,
            },
        ),
        (
            "android-spoofed-platform",
            {
                "platform": "ios",
                "capability": "aggregate",
                "permission_status": "granted",
                "status_observed_at": "2026-08-01T09:00:00Z",
                "collection_generation": 4,
            },
        ),
        (
            "android-unknown-permission",
            {
                "platform": "android",
                "capability": "aggregate",
                "permission_status": "unknown",
                "status_observed_at": "2026-08-01T09:00:00Z",
                "collection_generation": 4,
            },
        ),
    ),
    ids=("missing-fields", "spoofed-platform", "unknown-permission"),
)
def test_batch_rejects_malformed_persisted_android_boundary(
    client,
    session,
    device_id,
    status_payload,
):
    update_collection_status(
        session,
        device_id,
        ActivityCollectionStatusUpdate.model_validate(status_payload),
        now=datetime(2026, 8, 1, 9, tzinfo=UTC),
    )
    session.commit()

    response = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([SAMPLE_SLACK]),
            "device_id": device_id,
            "collection_generation": 4,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_android_boundary_invalid"
    assert (
        session.scalar(
            select(AppUsageSample).where(AppUsageSample.device_id == device_id)
        )
        is None
    )
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
        is None
    )


def test_revoke_blocks_delayed_grant_and_previous_generation_batch(
    client,
    session,
):
    granted = _set_generation(
        client,
        generation=20,
        observed_at="2026-08-01T10:00:00Z",
    )
    revoked = _set_generation(
        client,
        generation=21,
        observed_at="2026-08-01T10:05:00Z",
        granted=False,
    )
    delayed_grant = _set_generation(
        client,
        generation=20,
        observed_at="2026-08-01T10:06:00Z",
    )
    same_generation_grant = _set_generation(
        client,
        generation=21,
        observed_at="2026-08-01T10:07:00Z",
    )
    old_batch = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([SAMPLE_SLACK]),
            "collection_generation": 20,
        },
    )
    state = client.get(
        "/v1/activity/devices/pixel-8-test/collection"
    ).json()

    assert granted.status_code == 200
    assert revoked.status_code == 200
    assert delayed_grant.status_code == 200
    assert delayed_grant.json()["permission_status"] == "revoked"
    assert delayed_grant.json()["collection_generation"] == 21
    assert same_generation_grant.status_code == 200
    assert same_generation_grant.json()["permission_status"] == "revoked"
    assert same_generation_grant.json()["collection_generation"] == 21
    assert old_batch.status_code == 409
    assert old_batch.json()["error"]["code"] == "stale_collection_generation"
    assert state["permission_status"] == "revoked"
    assert state["collection_generation"] == 21
    assert session.scalar(select(AppUsageSample)) is None

    regranted = _set_generation(
        client,
        generation=22,
        observed_at="2026-08-01T10:10:00Z",
    )
    current_batch = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([SAMPLE_SLACK]),
            "collection_generation": 22,
        },
    )

    assert regranted.status_code == 200
    assert current_batch.status_code == 200
    assert current_batch.json()["accepted"] == 1


def test_pairing_revision_is_validated_inside_android_ingest_boundary(
    client,
    session,
):
    first = _set_generation(
        client,
        generation=30,
        pairing_revision=4,
        observed_at="2026-08-01T10:00:00Z",
    )
    advanced = _set_generation(
        client,
        generation=31,
        pairing_revision=5,
        observed_at="2026-08-01T10:05:00Z",
    )
    stale = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([SAMPLE_SLACK]),
            "collection_generation": 31,
            "pairing_revision": 4,
        },
    )
    current = client.post(
        "/v1/app-usage/batch",
        json={
            **_batch([SAMPLE_SLACK]),
            "collection_generation": 31,
            "pairing_revision": 5,
        },
    )

    assert first.status_code == 200
    assert advanced.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_pairing_revision"
    assert current.status_code == 200
    assert current.json()["accepted"] == 1
    rows = list(session.scalars(select(AppUsageSample)))
    assert len(rows) == 1
    assert rows[0].collection_generation == 31
