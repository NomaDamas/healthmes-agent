import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

import healthmes.activity.api as activity_api_module
from healthmes.activity.contracts import ActivityBatchIn
from healthmes.activity.repository import (
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    COLLECTION_CONFIG_EVENT,
    DAY_SUMMARY_EVENT,
    DELETION_TOMBSTONE_EVENT,
    IOS_SNAPSHOT_FENCE_EVENT,
    ActivityWriteConflictError,
)
from healthmes.activity.service import ingest_activity_batch
from healthmes.storage import update_retention_policy
from healthmes.store import WellnessEvent

IOS_KEY_FINGERPRINT = "1" * 40
IOS_KEY_ID = f"ios-key-{IOS_KEY_FINGERPRINT}"
IOS_APP_TOKEN = f"ios-app-v2-{IOS_KEY_FINGERPRINT}-" + ("a" * 40)


def _seed_legacy_ios_exclusion(
    client,
    session,
    device_id: str,
) -> None:
    configured = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={
            "platform": "android",
            "excluded_apps": ["com.example.private"],
        },
    )
    assert configured.status_code == 200
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == COLLECTION_CONFIG_EVENT,
            WellnessEvent.source_device == device_id,
        )
    )
    assert event is not None
    event.payload = {**event.payload, "platform": "ios"}
    session.commit()


def _hour_record(
    *,
    source_record_id: str,
    app_id: str,
    seconds: int,
) -> dict:
    return {
        "kind": "app_hour",
        "source_record_id": source_record_id,
        "bucket_start": "2026-08-01T10:00:00Z",
        "app_id": app_id,
        "foreground_seconds": seconds,
        "launches": 2,
        "category": "productivity",
        "coverage_seconds": 3600,
    }


def _activity_batch(
    records: list[dict],
    *,
    collection_revision: int = 0,
    source_device: str = "desktop-api-test",
) -> dict:
    return {
        "source_provider": "api-test-collector",
        "source_device": source_device,
        "platform": "macos",
        "capability": "aggregate",
        "timezone": "UTC",
        "collected_at": "2026-08-01T11:00:00Z",
        "collection_revision": collection_revision,
        "records": records,
    }


def _ios_snapshot(
    *,
    device_id: str,
    sequence: int,
    samples: list[dict],
    start: str = "2026-08-01T10:00:00Z",
    end: str = "2026-08-01T12:00:00Z",
    collected_at: str = "2026-08-01T13:00:00Z",
    authoritative_bucket_starts: list[str] | None = None,
) -> dict:
    authoritative = (
        sorted(
            {
                str(sample["bucket_start"])
                for sample in samples
            }
        )
        if authoritative_bucket_starts is None
        else authoritative_bucket_starts
    )
    return {
        "device_id": device_id,
        "timezone": "UTC",
        "capability": "aggregate",
        "permission_status": "granted",
        "pseudonym_key_id": IOS_KEY_ID,
        "collection_revision": 0,
        "collected_at": collected_at,
        "snapshot_sequence": sequence,
        "snapshot_start": start,
        "snapshot_end": end,
        "authoritative_bucket_starts": authoritative,
        "samples": samples,
    }


def _ios_sample(
    source_record_id: str,
    *,
    bucket_start: str,
    category: str,
    foreground_seconds: int,
) -> dict:
    return {
        "source_record_id": source_record_id,
        "bucket_start": bucket_start,
        "foreground_seconds": foreground_seconds,
        "category": category,
        "opaque_app_token": IOS_APP_TOKEN,
        "coverage_seconds": 3600,
    }


def test_collection_settings_filter_raw_events_and_context_identity(client, session) -> None:
    configured = client.put(
        "/v1/activity/devices/desktop-api-test/collection",
        json={
            "platform": "macos",
            "excluded_apps": ["private.app"],
        },
    )
    assert configured.status_code == 200
    assert configured.json()["config_revision"] == 1

    ingested = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="private",
                    app_id="PRIVATE.APP",
                    seconds=1200,
                ),
                _hour_record(
                    source_record_id="allowed",
                    app_id="editor.app",
                    seconds=1800,
                ),
            ],
            collection_revision=configured.json()["config_revision"],
        ),
    )

    assert ingested.status_code == 200
    assert ingested.json()["accepted"] == 1
    assert ingested.json()["excluded"] == 1
    rows = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
    )
    assert [row.payload["app_id"] for row in rows] == ["editor.app"]

    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-01", "timezone": "UTC"},
    )
    assert summary.status_code == 200
    assert summary.json()["total_active_minutes"] == 30.0
    assert "private.app" not in summary.text.casefold()
    assert "editor.app" not in summary.text.casefold()


def test_pause_and_resume_control_the_same_ingest_contract(client) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    paused = client.post(
        "/v1/activity/devices/desktop-api-test/pause",
        json={"until": until.isoformat()},
    )
    assert paused.status_code == 200
    assert paused.json()["effective_collecting"] is False
    assert paused.json()["blocked_reason"] == "collection_paused"

    blocked = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="paused",
                    app_id="editor.app",
                    seconds=100,
                )
            ],
            collection_revision=paused.json()["config_revision"],
        ),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "activity_collection_blocked"

    resumed = client.post("/v1/activity/devices/desktop-api-test/resume")
    assert resumed.status_code == 200
    assert resumed.json()["effective_collecting"] is True

    accepted = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="resumed",
                    app_id="editor.app",
                    seconds=100,
                )
            ],
            collection_revision=resumed.json()["config_revision"],
        ),
    )
    assert accepted.status_code == 200
    assert accepted.json()["created"] == 1


def test_activity_ingest_rejects_internal_provider_namespace(client) -> None:
    payload = _activity_batch(
        [
            _hour_record(
                source_record_id="spoofed-summary",
                app_id="editor.app",
                seconds=100,
            )
        ]
    )
    payload["source_provider"] = "healthmes-activity-aggregator"

    response = client.post("/v1/activity/events/batch", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_activity_ingest_requires_collection_revision(client) -> None:
    payload = _activity_batch(
        [
            _hour_record(
                source_record_id="missing-revision",
                app_id="editor.app",
                seconds=100,
            )
        ]
    )
    payload.pop("collection_revision")

    response = client.post("/v1/activity/events/batch", json=payload)

    assert response.status_code == 422
    assert (
        response.json()["error"]["code"]
        == "activity_collection_revision_required"
    )


def test_activity_ingest_rejects_built_in_adapter_provider_spoofing(client) -> None:
    for provider in ("activitywatch", "android-usage", "ios-device-activity"):
        payload = _activity_batch(
            [
                _hour_record(
                    source_record_id=f"spoof-{provider}",
                    app_id="editor.app",
                    seconds=100,
                )
            ]
        )
        payload["source_provider"] = provider

        response = client.post("/v1/activity/events/batch", json=payload)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "activity_provider_reserved"


def test_generic_source_ids_are_scoped_per_device(client, session) -> None:
    first = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="provider-local-hour",
                    app_id="editor.app",
                    seconds=600,
                )
            ],
            source_device="desktop-source-a",
        ),
    )
    second = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="provider-local-hour",
                    app_id="editor.app",
                    seconds=900,
                )
            ],
            source_device="desktop-source-b",
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )
    assert len(rows) == 2
    assert {row.source_device for row in rows} == {
        "desktop-source-a",
        "desktop-source-b",
    }
    assert len({row.source_record_id for row in rows}) == 2


def test_generic_source_scoping_preserves_same_device_legacy_retry(
    client,
    session,
) -> None:
    legacy = ActivityBatchIn.model_validate(
        _activity_batch(
            [
                _hour_record(
                    source_record_id="legacy-provider-local-hour",
                    app_id="editor.app",
                    seconds=600,
                )
            ],
            source_device="desktop-legacy-source",
        )
    )
    ingest_activity_batch(session, legacy)
    session.commit()

    retried = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="legacy-provider-local-hour",
                    app_id="editor.app",
                    seconds=600,
                )
            ],
            source_device="desktop-legacy-source",
        ),
    )

    assert retried.status_code == 200
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].source_record_id == "legacy-provider-local-hour"


def test_generic_source_scoping_preserves_legacy_deletion_tombstone(
    client,
    session,
) -> None:
    source_device = "desktop-legacy-deleted"
    source_record_id = "legacy-deleted-hour"
    legacy = ActivityBatchIn.model_validate(
        _activity_batch(
            [
                _hour_record(
                    source_record_id=source_record_id,
                    app_id="editor.app",
                    seconds=600,
                )
            ],
            source_device=source_device,
        )
    )
    ingest_activity_batch(session, legacy)
    session.commit()

    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": source_device,
            "start": "2026-08-01T10:00:00Z",
            "end": "2026-08-01T11:00:00Z",
            "include_summaries": True,
            "include_control": False,
            "confirm": True,
        },
    )
    replayed = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id=source_record_id,
                    app_id="editor.app",
                    seconds=600,
                )
            ],
            source_device=source_device,
        ),
    )

    assert deleted.status_code == 200
    assert deleted.json()["raw_events_deleted"] == 1
    assert replayed.status_code == 200
    assert replayed.json()["accepted"] == 0
    assert replayed.json()["tombstoned"] == 1
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_provider == legacy.source_provider,
                WellnessEvent.source_device == source_device,
            )
        )
        is None
    )


def test_generic_source_scoping_separates_other_device_from_legacy_id(
    client,
    session,
) -> None:
    legacy = ActivityBatchIn.model_validate(
        _activity_batch(
            [
                _hour_record(
                    source_record_id="shared-legacy-hour",
                    app_id="editor.app",
                    seconds=600,
                )
            ],
            source_device="desktop-legacy-owner",
        )
    )
    ingest_activity_batch(session, legacy)
    session.commit()

    created = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="shared-legacy-hour",
                    app_id="browser.app",
                    seconds=300,
                )
            ],
            source_device="desktop-new-owner",
        ),
    )

    assert created.status_code == 200
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )
    assert len(rows) == 2
    assert {row.source_device for row in rows} == {
        "desktop-legacy-owner",
        "desktop-new-owner",
    }
    assert len({row.source_record_id for row in rows}) == 2


def test_ios_unavailable_is_status_not_fake_zero_activity(client, session) -> None:
    response = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-api-test",
            "timezone": "Asia/Seoul",
            "capability": "unavailable",
            "permission_status": "unavailable",
            "reason": "screen_time_export_not_available",
            "samples": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert (
        session.scalar(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
        is None
    )
    status = client.get("/v1/activity/devices/iphone-api-test/collection")
    assert status.json()["capability"] == "unavailable"
    assert status.json()["permission_status"] == "unavailable"
    assert status.json()["effective_collecting"] is False
    assert status.json()["last_uploaded_at"] is not None
    assert status.json()["last_collected_at"] is None


def test_collection_state_exposes_raw_retention_cutoff(client, session) -> None:
    before = datetime.now(UTC)
    update_retention_policy(session, "activity_raw", "1d", now=before)
    session.commit()

    response = client.get(
        "/v1/activity/devices/iphone-retention-cutoff/collection"
    )

    assert response.status_code == 200
    cutoff = datetime.fromisoformat(response.json()["raw_retention_cutoff"])
    after = datetime.now(UTC)
    assert before - timedelta(days=1) <= cutoff
    assert cutoff <= after - timedelta(days=1)


def test_versioned_ios_collector_requires_explicit_input_registration(
    client,
) -> None:
    device_id = "ios-collector-v1-" + ("a" * 40)
    payload = _ios_snapshot(
        device_id=device_id,
        sequence=1,
        samples=[],
    )

    state = client.get(
        f"/v1/activity/devices/{device_id}/collection",
        params={"platform": "ios"},
    )
    blocked = client.post("/v1/activity/ios/report", json=payload)
    revision = client.get(
        "/v1/inputs/activity.ios-screentime"
    ).json()["revision"]
    registered = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        headers={"If-Match": f'"{revision}"'},
        json={
            "instance_id": device_id,
            "platform": "ios",
            "enabled": True,
        },
    )
    accepted = client.post(
        "/v1/activity/ios/report",
        json={**payload, "collection_revision": 1},
    )

    assert state.status_code == 200
    assert state.json()["enabled"] is False
    assert state.json()["blocked_reason"] == "collection_disabled"
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "activity_collection_blocked"
    assert registered.status_code == 200
    assert accepted.status_code == 200


def test_collection_settings_reject_invalid_ios_exclusion_namespaces(
    client,
) -> None:
    configured = client.put(
        "/v1/activity/devices/iphone-private-settings/collection",
        json={"platform": "ios"},
    )
    assert configured.status_code == 200

    invalid_sets = (
        ["com.example.private"],
        ["ios-app-" + ("a" * 40)],
        [
            IOS_APP_TOKEN,
            "ios-app-v2-" + ("2" * 40) + "-" + ("b" * 40),
        ],
    )

    for excluded_apps in invalid_sets:
        response = client.put(
            "/v1/activity/devices/iphone-private-settings/collection",
            json={"excluded_apps": excluded_apps},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_ios_app_token"


def test_platform_change_to_ios_requires_clearing_legacy_exclusions(
    client,
) -> None:
    device_id = "phone-platform-transition"
    android = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={
            "platform": "android",
            "excluded_apps": ["com.example.private"],
        },
    )
    assert android.status_code == 200

    rejected = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={"platform": "ios"},
    )
    accepted = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={
            "platform": "ios",
            "excluded_apps": [],
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_ios_app_token"
    assert accepted.status_code == 200
    assert accepted.json()["platform"] == "ios"
    assert accepted.json()["excluded_apps"] == []
    assert accepted.json()["ios_pseudonym_key_id"] is None


def test_legacy_ios_exclusions_allow_only_safe_recovery_controls(
    client,
    session,
) -> None:
    device_id = "iphone-legacy-exclusion-recovery"
    _seed_legacy_ios_exclusion(client, session, device_id)

    disabled = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={"enabled": False},
    )
    rejected_enable = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={"enabled": True},
    )
    paused = client.post(
        f"/v1/activity/devices/{device_id}/pause",
        json={
            "until": (
                datetime.now(UTC) + timedelta(hours=1)
            ).isoformat()
        },
    )
    rejected_resume = client.post(
        f"/v1/activity/devices/{device_id}/resume",
    )
    cleared = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={"excluded_apps": []},
    )
    resumed = client.post(
        f"/v1/activity/devices/{device_id}/resume",
    )

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert rejected_enable.status_code == 422
    assert rejected_enable.json()["error"]["code"] == "invalid_ios_app_token"
    assert paused.status_code == 200
    assert paused.json()["blocked_reason"] == "collection_disabled"
    assert rejected_resume.status_code == 422
    assert rejected_resume.json()["error"]["code"] == "invalid_ios_app_token"
    assert cleared.status_code == 200
    assert cleared.json()["excluded_apps"] == []
    assert resumed.status_code == 200


def test_available_ios_report_rejects_legacy_exclusion_without_key(
    client,
    session,
) -> None:
    device_id = "iphone-legacy-exclusion-report"
    _seed_legacy_ios_exclusion(client, session, device_id)

    response = client.post(
        "/v1/activity/ios/report",
        json=_ios_snapshot(
            device_id=device_id,
            sequence=1,
            samples=[],
        ),
    )

    assert response.status_code == 409
    assert (
        response.json()["error"]["code"]
        == "ios_exclusion_reapproval_required"
    )


def test_ios_restricted_is_blocked_status_not_fake_zero_activity(
    client,
    session,
) -> None:
    response = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-restricted-api",
            "timezone": "Asia/Seoul",
            "capability": "aggregate",
            "permission_status": "restricted",
            "reason": "family_controls_restricted",
            "samples": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
        is None
    )
    status = client.get(
        "/v1/activity/devices/iphone-restricted-api/collection"
    ).json()
    assert status["capability"] == "aggregate"
    assert status["permission_status"] == "restricted"
    assert status["effective_collecting"] is False
    assert status["blocked_reason"] == "permission_restricted"
    assert status["last_uploaded_at"] is not None
    assert status["last_collected_at"] is None


def test_ios_restricted_report_rejects_fake_zero_sample(client, session) -> None:
    response = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-restricted-sample",
            "timezone": "UTC",
            "capability": "aggregate",
            "permission_status": "restricted",
            "reason": "family_controls_restricted",
            "samples": [
                {
                    "source_record_id": "fake-zero",
                    "bucket_start": "2026-08-01T10:00:00Z",
                    "foreground_seconds": 0,
                    "category": "other",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
        is None
    )


def test_android_permission_status_can_recover_from_revoked_to_granted(client) -> None:
    revoked = client.post(
        "/v1/activity/devices/android-permission-recovery/status",
        json={
            "platform": "android",
            "capability": "aggregate",
            "permission_status": "revoked",
            "status_reason": "usage_access_revoked",
            "status_observed_at": "2026-08-01T12:00:00Z",
            "collection_generation": 1,
            "queue_depth": 0,
        },
    )

    assert revoked.status_code == 200
    assert revoked.json()["capability"] == "aggregate"
    assert revoked.json()["permission_status"] == "revoked"
    assert revoked.json()["effective_collecting"] is False
    assert revoked.json()["blocked_reason"] == "permission_revoked"

    granted = client.post(
        "/v1/activity/devices/android-permission-recovery/status",
        json={
            "platform": "android",
            "capability": "aggregate",
            "permission_status": "granted",
            "status_reason": None,
            "status_observed_at": "2026-08-01T12:05:00Z",
            "collection_generation": 2,
            "queue_depth": 0,
        },
    )

    assert granted.status_code == 200
    assert granted.json()["capability"] == "aggregate"
    assert granted.json()["permission_status"] == "granted"
    assert granted.json()["status_reason"] is None
    assert granted.json()["effective_collecting"] is True
    assert granted.json()["blocked_reason"] is None


def test_ios_permission_status_can_recover_with_new_generation(client) -> None:
    device_id = "iphone-permission-recovery"
    denied = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": device_id,
            "timezone": "UTC",
            "capability": "unavailable",
            "permission_status": "denied",
            "reason": "ios_screen_time_permission_denied",
            "collected_at": "2026-08-01T12:00:00Z",
            "collection_revision": 0,
            "collection_generation": 1,
            "samples": [],
        },
    )

    assert denied.status_code == 200
    state = client.get(
        f"/v1/activity/devices/{device_id}/collection"
    ).json()
    assert state["permission_status"] == "denied"
    assert state["collection_generation"] == 1
    assert state["effective_collecting"] is False
    assert state["blocked_reason"] == "permission_denied"

    granted = client.post(
        "/v1/activity/ios/report",
        json={
            **_ios_snapshot(
                device_id=device_id,
                sequence=1,
                start="2026-08-01T12:00:00Z",
                end="2026-08-01T13:00:00Z",
                collected_at="2026-08-01T13:05:00Z",
                samples=[],
            ),
            "collection_generation": 2,
        },
    )

    assert granted.status_code == 200
    state = client.get(
        f"/v1/activity/devices/{device_id}/collection"
    ).json()
    assert state["permission_status"] == "granted"
    assert state["collection_generation"] == 2
    assert state["effective_collecting"] is True
    assert state["blocked_reason"] is None


@pytest.mark.parametrize(
    "payload",
    (
        {"collection_generation": 1},
        {"pairing_revision": 1},
        {
            "platform": "ios",
            "capability": "aggregate",
            "permission_status": "granted",
            "status_observed_at": "2026-08-01T12:00:00Z",
            "collection_generation": 1,
        },
        {
            "platform": "android",
            "capability": "detailed",
            "permission_status": "granted",
            "status_observed_at": "2026-08-01T12:00:00Z",
            "collection_generation": 1,
        },
        {
            "platform": "android",
            "permission_status": "granted",
            "status_observed_at": "2026-08-01T12:00:00Z",
            "collection_generation": 1,
        },
    ),
    ids=(
        "generation-only",
        "pairing-only",
        "ios-spoof",
        "wrong-capability",
        "missing-capability",
    ),
)
def test_public_generation_status_requires_complete_android_boundary(
    client,
    session,
    payload,
) -> None:
    device_id = "android-incomplete-boundary"

    response = client.post(
        f"/v1/activity/devices/{device_id}/status",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "activity_status_boundary_required"
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_device == device_id,
            )
        )
        is None
    )


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    (
        ("GET", "collection", None),
        ("PUT", "collection", {}),
        (
            "POST",
            "pause",
            {"until": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        ),
        ("POST", "resume", None),
        ("POST", "status", {}),
    ),
)
def test_collection_device_paths_reject_ids_longer_than_storage_column(
    client,
    session,
    method,
    suffix,
    body,
) -> None:
    device_id = "d" * 256

    response = client.request(
        method,
        f"/v1/activity/devices/{device_id}/{suffix}",
        json=body,
    )

    assert response.status_code == 422
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_device == device_id,
            )
        )
        is None
    )


def test_ios_aggregate_hour_can_be_revised_without_duplicate_rows(client, session) -> None:
    payload = {
        "device_id": "iphone-api-test",
        "timezone": "Asia/Seoul",
        "capability": "aggregate",
        "permission_status": "granted",
        "pseudonym_key_id": IOS_KEY_ID,
        "collection_revision": 0,
        "samples": [
            {
                "source_record_id": "screen-time-hour-1",
                "bucket_start": "2026-08-01T10:00:00Z",
                "foreground_seconds": 600,
                "category": "social",
                "opaque_app_token": IOS_APP_TOKEN,
                "coverage_seconds": 3600,
            }
        ],
    }
    first = client.post("/v1/activity/ios/report", json=payload)
    revised = {
        **payload,
        "samples": [
            {
                **payload["samples"][0],
                "foreground_seconds": 900,
            }
        ],
    }
    second = client.post("/v1/activity/ios/report", json=revised)

    assert first.status_code == 200
    assert first.json()["created"] == 1
    assert second.status_code == 200
    assert second.json()["updated"] == 1
    rows = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
    )
    assert len(rows) == 1
    assert rows[0].payload["foreground_seconds"] == 900
    assert rows[0].payload["app_id"] == IOS_APP_TOKEN


def test_ios_missing_launches_remains_unknown_in_storage_summary_and_focus(
    client,
    session,
) -> None:
    payload = _ios_snapshot(
        device_id="iphone-launches-unknown",
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        samples=[
            {
                "source_record_id": "screen-time-hour",
                "bucket_start": "2026-08-13T10:00:00Z",
                "foreground_seconds": 1800,
                "category": "social",
                "opaque_app_token": IOS_APP_TOKEN,
                "coverage_seconds": 3600,
            }
        ],
    )

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 200
    row = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT,
            WellnessEvent.source_device == payload["device_id"],
        )
    )
    assert row is not None
    assert row.payload["launches"] == 0
    assert row.payload["launches_observed"] is False

    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-13", "timezone": "UTC"},
    )
    assert summary.status_code == 200
    assert summary.json()["app_launches_or_switches"] == 0
    assert summary.json()["app_launches_or_switches_range"] == {
        "lower_bound": 0,
        "upper_bound": None,
        "precision": "unknown",
    }
    assert (
        "launches_unavailable_for_some_sources"
        in summary.json()["limitations"]
    )

    focus = client.get(
        "/v1/activity/focus-context",
        params={
            "start": "2026-08-13T10:00:00Z",
            "end": "2026-08-13T11:00:00Z",
            "timezone": "UTC",
        },
    )
    assert focus.status_code == 200
    assert (
        focus.json()["metrics"]["launches_or_switches_per_active_hour"]
        is None
    )
    assert (
        "launches_unavailable_for_some_sources"
        in focus.json()["limitations"]
    )


def test_ios_coverage_only_hour_records_observed_zero_usage(
    client,
    session,
) -> None:
    payload = _ios_snapshot(
        device_id="iphone-zero-usage-hour",
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        samples=[
            {
                "source_record_id": "coverage-only-hour",
                "bucket_start": "2026-08-13T10:00:00Z",
                "foreground_seconds": 0,
                "category": None,
                "opaque_app_token": None,
                "coverage_seconds": 3600,
                "coverage_only": True,
            }
        ],
    )

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 200
    row = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT,
            WellnessEvent.source_device == payload["device_id"],
        )
    )
    assert row is not None
    assert row.payload["coverage_only"] is True
    assert row.payload["app_id"] == "__healthmes_coverage__"
    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-13", "timezone": "UTC"},
    )
    assert summary.status_code == 200
    assert summary.json()["status"] == "ok"
    assert summary.json()["total_active_minutes"] == 0
    assert summary.json()["source_coverage"]["ratio"] > 0


def test_ios_coverage_only_sample_rejects_identity_or_activity(client) -> None:
    payload = _ios_snapshot(
        device_id="iphone-invalid-coverage-hour",
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        samples=[
            {
                "source_record_id": "invalid-coverage-only-hour",
                "bucket_start": "2026-08-13T10:00:00Z",
                "foreground_seconds": 1,
                "category": "social",
                "opaque_app_token": IOS_APP_TOKEN,
                "coverage_seconds": 3600,
                "coverage_only": True,
            }
        ],
    )

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 422


def test_ios_activity_sample_requires_token_when_exclusions_are_configured(
    client,
    session,
) -> None:
    device_id = "iphone-tokenless-exclusion-bypass"
    configured = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={
            "platform": "ios",
            "excluded_apps": [IOS_APP_TOKEN],
        },
    )
    assert configured.status_code == 200
    payload = _ios_snapshot(
        device_id=device_id,
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        samples=[
            {
                "source_record_id": "tokenless-private-app",
                "bucket_start": "2026-08-13T10:00:00Z",
                "foreground_seconds": 600,
                "category": "social",
                "coverage_seconds": 3600,
            }
        ],
    )
    payload["collection_revision"] = configured.json()[
        "config_revision"
    ]

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT,
            WellnessEvent.source_device == device_id,
        )
    ) is None


def test_ios_report_rejects_mixed_pseudonym_key_namespaces(client) -> None:
    payload = _ios_snapshot(
        device_id="iphone-mixed-pseudonym-keys",
        sequence=1,
        samples=[
            {
                **_ios_sample(
                    "mixed-key-a",
                    bucket_start="2026-08-01T10:00:00Z",
                    category="social",
                    foreground_seconds=600,
                ),
                "opaque_app_token": IOS_APP_TOKEN,
            },
            {
                **_ios_sample(
                    "mixed-key-b",
                    bucket_start="2026-08-01T11:00:00Z",
                    category="productivity",
                    foreground_seconds=600,
                ),
                "opaque_app_token": (
                    "ios-app-v2-" + ("2" * 40) + "-" + ("b" * 40)
                ),
            },
        ],
    )

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_ios_report_requires_the_configured_exclusion_key_namespace(
    client,
) -> None:
    device_id = "iphone-configured-key-mismatch"
    configured = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={
            "platform": "ios",
            "excluded_apps": [IOS_APP_TOKEN],
        },
    )
    assert configured.status_code == 200
    other_key_id = "ios-key-" + ("2" * 40)

    payload = _ios_snapshot(
        device_id=device_id,
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        authoritative_bucket_starts=[
            "2026-08-13T10:00:00Z",
        ],
        samples=[],
    )
    payload["collection_revision"] = configured.json()["config_revision"]
    payload["pseudonym_key_id"] = other_key_id

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 409
    assert (
        response.json()["error"]["code"]
        == "ios_exclusion_reapproval_required"
    )


def test_ios_report_rejects_unobservable_launch_count(client) -> None:
    payload = _ios_snapshot(
        device_id="iphone-invalid-launches",
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        samples=[
            {
                "source_record_id": "invalid-launch-count",
                "bucket_start": "2026-08-13T10:00:00Z",
                "foreground_seconds": 600,
                "category": "social",
                "launches": 0,
                "coverage_seconds": 3600,
            }
        ],
    )

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "category",
    (
        "com.private.bank.app",
        "Private Banking",
        "ios-category-private",
    ),
)
def test_ios_report_rejects_noncanonical_category_identity(
    client,
    category: str,
) -> None:
    payload = _ios_snapshot(
        device_id=f"iphone-private-category-{category[:8]}",
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        samples=[
            {
                "source_record_id": "private-category",
                "bucket_start": "2026-08-13T10:00:00Z",
                "foreground_seconds": 600,
                "category": category,
                "coverage_seconds": 3600,
            }
        ],
    )

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_ios_report_accepts_opaque_category_token(client) -> None:
    payload = _ios_snapshot(
        device_id="iphone-opaque-category",
        sequence=1,
        start="2026-08-13T10:00:00Z",
        end="2026-08-13T11:00:00Z",
        collected_at="2026-08-13T12:00:00Z",
        samples=[
            {
                "source_record_id": "opaque-category",
                "bucket_start": "2026-08-13T10:00:00Z",
                "foreground_seconds": 600,
                "category": "ios-category-" + ("a" * 40),
                "opaque_app_token": IOS_APP_TOKEN,
                "coverage_seconds": 3600,
            }
        ],
    )

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 200
    assert response.json()["created"] == 1


def test_ios_snapshot_bounds_align_to_report_timezone_not_utc(client) -> None:
    response = client.post(
        "/v1/activity/ios/report",
        json={
            **_ios_snapshot(
                device_id="iphone-local-hour-boundary",
                sequence=1,
                start="2026-08-13T04:15:00Z",
                end="2026-08-13T05:15:00Z",
                collected_at="2026-08-13T06:00:00Z",
                samples=[
                    {
                        "source_record_id": "local-hour",
                        "bucket_start": "2026-08-13T04:15:00Z",
                        "foreground_seconds": 600,
                        "category": "productivity",
                        "opaque_app_token": IOS_APP_TOKEN,
                        "coverage_seconds": 3600,
                    }
                ],
            ),
            "timezone": "Asia/Kathmandu",
        },
    )

    assert response.status_code == 200
    assert response.json()["created"] == 1


def test_ios_snapshot_accepts_lord_howe_half_hour_fallback_bucket(
    client,
    session,
) -> None:
    update_retention_policy(
        session,
        "activity_raw",
        "forever",
        now=datetime(2026, 8, 14, 12, tzinfo=UTC),
    )
    session.commit()
    response = client.post(
        "/v1/activity/ios/report",
        json={
            **_ios_snapshot(
                device_id="iphone-lord-howe-fallback",
                sequence=1,
                start="2026-04-04T14:30:00Z",
                end="2026-04-04T15:30:00Z",
                collected_at="2026-04-04T16:00:00Z",
                samples=[
                    {
                        "source_record_id": "lord-howe-hour",
                        "bucket_start": "2026-04-04T14:30:00Z",
                        "foreground_seconds": 600,
                        "category": "productivity",
                        "opaque_app_token": IOS_APP_TOKEN,
                        "coverage_seconds": 3600,
                    }
                ],
            ),
            "timezone": "Australia/Lord_Howe",
        },
    )

    assert response.status_code == 200
    assert response.json()["created"] == 1


def test_ios_stale_aggregate_cannot_overwrite_newer_snapshot(
    client,
    session,
) -> None:
    payload = {
        "device_id": "iphone-stale-report",
        "timezone": "UTC",
        "capability": "aggregate",
        "permission_status": "granted",
        "pseudonym_key_id": IOS_KEY_ID,
        "collection_revision": 0,
        "collected_at": "2026-08-01T12:00:00Z",
        "samples": [
            {
                "source_record_id": "screen-time-hour-1",
                "bucket_start": "2026-08-01T10:00:00Z",
                "foreground_seconds": 900,
                "category": "social",
                "opaque_app_token": IOS_APP_TOKEN,
                "coverage_seconds": 3600,
            }
        ],
    }
    newest = client.post("/v1/activity/ios/report", json=payload)
    stale = client.post(
        "/v1/activity/ios/report",
        json={
            **payload,
            "collected_at": "2026-08-01T11:00:00Z",
            "samples": [
                {
                    **payload["samples"][0],
                    "foreground_seconds": 600,
                },
                {
                    **payload["samples"][0],
                    "source_record_id": "late-new-source",
                    "foreground_seconds": 300,
                },
            ],
        },
    )

    assert newest.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "activity_source_conflict"

    row = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT,
            WellnessEvent.source_device == payload["device_id"],
        )
    )
    assert row is not None
    assert row.payload["foreground_seconds"] == 900
    assert row.payload["launches"] == 0
    assert row.payload["launches_observed"] is False
    assert session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == APP_HOUR_EVENT,
            WellnessEvent.source_device == payload["device_id"],
        )
        .offset(1)
    ) is None


def test_ios_newer_authoritative_snapshot_deletes_missing_rows(
    client,
    session,
) -> None:
    device_id = "iphone-authoritative-delete"
    first = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[
            _ios_sample(
                "social-hour",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            ),
            _ios_sample(
                "work-hour",
                bucket_start="2026-08-01T11:00:00Z",
                category="productivity",
                foreground_seconds=1200,
            ),
        ],
    )
    second = _ios_snapshot(
        device_id=device_id,
        sequence=101,
        collected_at="2026-08-01T13:05:00Z",
        authoritative_bucket_starts=[
            "2026-08-01T10:00:00Z",
            "2026-08-01T11:00:00Z",
        ],
        samples=[first["samples"][0]],
    )

    created = client.post("/v1/activity/ios/report", json=first)
    replaced = client.post("/v1/activity/ios/report", json=second)

    assert created.status_code == 200
    assert created.json()["created"] == 2
    assert replaced.status_code == 200
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].payload["app_id"] == IOS_APP_TOKEN


def test_ios_snapshot_preserves_hours_not_marked_authoritative(
    client,
    session,
) -> None:
    device_id = "iphone-partial-authoritative"
    initial = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[
            _ios_sample(
                "first-hour",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            ),
            _ios_sample(
                "second-hour",
                bucket_start="2026-08-01T11:00:00Z",
                category="productivity",
                foreground_seconds=1200,
            ),
        ],
    )
    partial = _ios_snapshot(
        device_id=device_id,
        sequence=101,
        collected_at="2026-08-01T13:05:00Z",
        authoritative_bucket_starts=[
            "2026-08-01T10:00:00Z",
        ],
        samples=[],
    )

    assert client.post(
        "/v1/activity/ios/report",
        json=initial,
    ).status_code == 200
    replaced = client.post(
        "/v1/activity/ios/report",
        json=partial,
    )

    assert replaced.status_code == 200
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].observed_at.replace(tzinfo=UTC) == datetime(
        2026,
        8,
        1,
        11,
        tzinfo=UTC,
    )
    assert rows[0].payload["app_id"] == IOS_APP_TOKEN


def test_ios_newer_empty_authoritative_snapshot_deletes_range(
    client,
    session,
) -> None:
    device_id = "iphone-authoritative-empty"
    initial = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[
            _ios_sample(
                "social-hour",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            )
        ],
    )
    empty = _ios_snapshot(
        device_id=device_id,
        sequence=101,
        collected_at="2026-08-01T13:05:00Z",
        authoritative_bucket_starts=[
            "2026-08-01T10:00:00Z",
        ],
        samples=[],
    )

    assert client.post("/v1/activity/ios/report", json=initial).status_code == 200
    deleted = client.post("/v1/activity/ios/report", json=empty)

    assert deleted.status_code == 200
    assert deleted.json()["accepted"] == 0
    assert deleted.json()["affected_dates"] == ["2026-08-01"]
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
        is None
    )


def test_ios_stale_authoritative_snapshot_cannot_resurrect_deleted_row(
    client,
    session,
) -> None:
    device_id = "iphone-authoritative-stale"
    original = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[
            _ios_sample(
                "keep",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            ),
            _ios_sample(
                "remove",
                bucket_start="2026-08-01T11:00:00Z",
                category="video",
                foreground_seconds=600,
            ),
        ],
    )
    latest = _ios_snapshot(
        device_id=device_id,
        sequence=101,
        collected_at="2026-08-01T13:05:00Z",
        authoritative_bucket_starts=[
            "2026-08-01T10:00:00Z",
            "2026-08-01T11:00:00Z",
        ],
        samples=[original["samples"][0]],
    )

    assert client.post("/v1/activity/ios/report", json=original).status_code == 200
    assert client.post("/v1/activity/ios/report", json=latest).status_code == 200
    stale = client.post("/v1/activity/ios/report", json=original)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "activity_source_conflict"
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].payload["app_id"] == IOS_APP_TOKEN


def test_ios_equal_authoritative_sequence_is_idempotent_only_for_same_content(
    client,
    session,
) -> None:
    device_id = "iphone-authoritative-equal"
    payload = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[
            _ios_sample(
                "social-hour",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            )
        ],
    )

    first = client.post("/v1/activity/ios/report", json=payload)
    replay = client.post("/v1/activity/ios/report", json=payload)
    changed = client.post(
        "/v1/activity/ios/report",
        json={
            **payload,
            "samples": [
                {
                    **payload["samples"][0],
                    "foreground_seconds": 600,
                }
            ],
        },
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "activity_source_conflict"
    row = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT,
            WellnessEvent.source_device == device_id,
        )
    )
    assert row is not None
    assert row.payload["foreground_seconds"] == 900


def test_ios_new_install_requires_authenticated_generation_reset(
    client,
    session,
) -> None:
    device_id = "iphone-authoritative-reinstall"
    first = {
        **_ios_snapshot(
            device_id=device_id,
            sequence=100,
            samples=[
                _ios_sample(
                    "same-install-hour",
                    bucket_start="2026-08-01T10:00:00Z",
                    category="social",
                    foreground_seconds=900,
                )
            ],
        ),
        "collection_generation": 1,
    }
    missing_reset = {
        **_ios_snapshot(
            device_id=device_id,
            sequence=1,
            collected_at="2026-08-01T13:05:00Z",
            samples=[
                _ios_sample(
                    "same-install-hour",
                    bucket_start="2026-08-01T10:00:00Z",
                    category="productivity",
                    foreground_seconds=1200,
                )
            ],
        ),
        "collection_generation": 2,
    }
    reset = {
        **missing_reset,
        "reset_snapshot_fence": True,
    }
    stale_old_install = {
        **first,
        "snapshot_sequence": 101,
        "collected_at": "2026-08-01T13:10:00Z",
    }

    assert client.post("/v1/activity/ios/report", json=first).status_code == 200
    rejected = client.post(
        "/v1/activity/ios/report",
        json=missing_reset,
    )
    accepted = client.post("/v1/activity/ios/report", json=reset)
    stale = client.post(
        "/v1/activity/ios/report",
        json=stale_old_install,
    )

    assert rejected.status_code == 409
    assert (
        rejected.json()["error"]["code"]
        == "activity_snapshot_fence_reset_required"
    )
    assert accepted.status_code == 200
    assert stale.status_code == 409
    assert (
        stale.json()["error"]["code"]
        == "activity_snapshot_fence_reset_required"
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].payload["app_id"] == IOS_APP_TOKEN
    state = client.get(
        f"/v1/activity/devices/{device_id}/collection"
    ).json()
    assert state["collection_generation"] == 2


def test_ios_successful_fence_reset_replay_precedes_mutable_collection_gate(
    client,
) -> None:
    device_id = "iphone-authoritative-reset-replay"
    original = {
        **_ios_snapshot(
            device_id=device_id,
            sequence=100,
            samples=[
                _ios_sample(
                    "old-install-hour",
                    bucket_start="2026-08-01T10:00:00Z",
                    category="social",
                    foreground_seconds=900,
                )
            ],
        ),
        "collection_generation": 1,
    }
    reset = {
        **_ios_snapshot(
            device_id=device_id,
            sequence=1,
            collected_at="2026-08-01T13:05:00Z",
            samples=[
                _ios_sample(
                    "new-install-hour",
                    bucket_start="2026-08-01T10:00:00Z",
                    category="productivity",
                    foreground_seconds=1200,
                )
            ],
        ),
        "collection_generation": 2,
        "reset_snapshot_fence": True,
    }

    assert client.post(
        "/v1/activity/ios/report",
        json=original,
    ).status_code == 200
    accepted = client.post("/v1/activity/ios/report", json=reset)
    disabled = client.put(
        f"/v1/activity/devices/{device_id}/collection",
        json={"platform": "ios", "enabled": False},
    )
    replay = client.post("/v1/activity/ios/report", json=reset)

    assert accepted.status_code == 200
    assert disabled.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == accepted.json()


def test_ios_legacy_fence_replay_reports_unavailable_exact_response(
    client,
    session,
) -> None:
    device_id = "iphone-authoritative-legacy-upgrade"
    legacy = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[],
    )
    upgraded = {
        **_ios_snapshot(
            device_id=device_id,
            sequence=1,
            collected_at="2026-08-01T13:05:00Z",
            samples=[],
        ),
        "collection_generation": 1,
    }

    assert client.post("/v1/activity/ios/report", json=legacy).status_code == 200
    fence = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == IOS_SNAPSHOT_FENCE_EVENT,
            WellnessEvent.source_device == device_id,
        )
    )
    assert fence is not None
    fence.payload = {
        key: value
        for key, value in fence.payload.items()
        if key != "accepted_response"
    }
    session.commit()

    replay = client.post("/v1/activity/ios/report", json=legacy)
    assert replay.status_code == 409
    assert (
        replay.json()["error"]["code"]
        == "snapshot_retry_response_unavailable"
    )
    rejected = client.post("/v1/activity/ios/report", json=upgraded)
    accepted = client.post(
        "/v1/activity/ios/report",
        json={**upgraded, "reset_snapshot_fence": True},
    )

    assert rejected.status_code == 409
    assert accepted.status_code == 200


def test_ios_equal_authoritative_replay_does_not_advance_data_freshness(
    client,
) -> None:
    device_id = "iphone-authoritative-freshness"
    payload = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        collected_at="2026-08-01T13:00:00Z",
        samples=[
            _ios_sample(
                "social-hour",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            )
        ],
    )

    first = client.post("/v1/activity/ios/report", json=payload)
    replay = client.post(
        "/v1/activity/ios/report",
        json={
            **payload,
            "collected_at": "2026-08-01T14:00:00Z",
        },
    )
    status = client.get(
        f"/v1/activity/devices/{device_id}/collection"
    ).json()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert status["last_collected_at"] == "2026-08-01T13:00:00Z"
    assert status["status_observed_at"] == "2026-08-01T13:00:00Z"


def test_ios_legacy_samples_cannot_mix_after_authoritative_mode(
    client,
    session,
) -> None:
    device_id = "iphone-authoritative-no-legacy-mix"
    authoritative = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[
            _ios_sample(
                "social-hour",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            )
        ],
    )
    assert (
        client.post(
            "/v1/activity/ios/report",
            json=authoritative,
        ).status_code
        == 200
    )

    legacy = {
        key: value
        for key, value in authoritative.items()
        if key not in {"snapshot_sequence", "snapshot_start", "snapshot_end"}
    }
    legacy["collected_at"] = "2026-08-01T13:05:00Z"
    legacy["samples"] = [
        _ios_sample(
            "late-legacy-row",
            bucket_start="2026-08-01T11:00:00Z",
            category="video",
            foreground_seconds=600,
        )
    ]

    response = client.post("/v1/activity/ios/report", json=legacy)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_source_conflict"
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].payload["app_id"] == IOS_APP_TOKEN


def test_ios_authoritative_write_conflict_rolls_back_entire_range(
    client,
    session,
    monkeypatch,
) -> None:
    device_id = "iphone-authoritative-rollback"
    original = _ios_snapshot(
        device_id=device_id,
        sequence=100,
        samples=[
            _ios_sample(
                "first",
                bucket_start="2026-08-01T10:00:00Z",
                category="social",
                foreground_seconds=900,
            ),
            _ios_sample(
                "second",
                bucket_start="2026-08-01T11:00:00Z",
                category="video",
                foreground_seconds=600,
            ),
        ],
    )
    assert client.post("/v1/activity/ios/report", json=original).status_code == 200

    def fail_fence(*args, **kwargs):
        raise ActivityWriteConflictError("simulated iOS fence race")

    monkeypatch.setattr(
        activity_api_module,
        "persist_ios_snapshot_fence",
        fail_fence,
    )
    replacement = _ios_snapshot(
        device_id=device_id,
        sequence=101,
        collected_at="2026-08-01T13:05:00Z",
        samples=[
            _ios_sample(
                "replacement",
                bucket_start="2026-08-01T10:00:00Z",
                category="productivity",
                foreground_seconds=1200,
            )
        ],
    )

    response = client.post("/v1/activity/ios/report", json=replacement)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_write_conflict"
    session.expire_all()
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT,
                WellnessEvent.source_device == device_id,
            )
        )
    )
    assert sorted(row.payload["category"] for row in rows) == [
        "social",
        "video",
    ]


@pytest.mark.parametrize(
    "updates",
    (
        {"snapshot_end": None},
        {"snapshot_start": "2026-08-01T10:30:00Z"},
        {"snapshot_end": "2026-08-01T10:00:00Z"},
        {
            "samples": [
                _ios_sample(
                    "duplicate",
                    bucket_start="2026-08-01T10:00:00Z",
                    category="social",
                    foreground_seconds=600,
                ),
                _ios_sample(
                    "duplicate",
                    bucket_start="2026-08-01T11:00:00Z",
                    category="video",
                    foreground_seconds=600,
                ),
            ]
        },
        {
            "samples": [
                _ios_sample(
                    "outside",
                    bucket_start="2026-08-01T12:00:00Z",
                    category="social",
                    foreground_seconds=600,
                )
            ]
        },
    ),
    ids=(
        "partial-fence",
        "unaligned-range",
        "empty-range",
        "duplicate-source-id",
        "sample-outside-range",
    ),
)
def test_ios_authoritative_snapshot_contract_rejects_invalid_manifest(
    client,
    updates,
) -> None:
    payload = _ios_snapshot(
        device_id="iphone-authoritative-invalid",
        sequence=100,
        samples=[],
    )
    payload.update(updates)

    response = client.post("/v1/activity/ios/report", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_ios_samples_require_collection_revision(client) -> None:
    response = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-missing-revision",
            "timezone": "UTC",
            "capability": "aggregate",
            "permission_status": "granted",
            "pseudonym_key_id": IOS_KEY_ID,
            "samples": [
                {
                    "source_record_id": "screen-time-hour",
                    "bucket_start": "2026-08-01T10:00:00Z",
                    "foreground_seconds": 600,
                    "category": "social",
                    "opaque_app_token": IOS_APP_TOKEN,
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_ios_samples_use_caller_revision_instead_of_server_injection(
    client,
    session,
) -> None:
    configured = client.put(
        "/v1/activity/devices/iphone-stale-revision/collection",
        json={
            "platform": "ios",
            "excluded_apps": [IOS_APP_TOKEN],
        },
    )
    assert configured.status_code == 200
    assert configured.json()["config_revision"] == 1

    response = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-stale-revision",
            "timezone": "UTC",
            "capability": "aggregate",
            "permission_status": "granted",
            "pseudonym_key_id": IOS_KEY_ID,
            "collection_revision": 0,
            "samples": [
                {
                    "source_record_id": "screen-time-hour",
                    "bucket_start": "2026-08-01T10:00:00Z",
                    "foreground_seconds": 600,
                    "category": "social",
                    "opaque_app_token": IOS_APP_TOKEN,
                }
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_collection_revision"
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
        is None
    )
    status = client.get(
        "/v1/activity/devices/iphone-stale-revision/collection"
    ).json()
    assert status["last_uploaded_at"] is None


def test_ios_report_rejects_future_collected_at_before_status_update(client) -> None:
    response = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-future-clock",
            "timezone": "UTC",
            "capability": "unavailable",
            "permission_status": "unavailable",
            "reason": "not_available",
            "collected_at": "2100-01-01T00:00:00Z",
            "samples": [],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_future_data"
    status = client.get(
        "/v1/activity/devices/iphone-future-clock/collection"
    ).json()
    assert status["last_uploaded_at"] is None


def test_collection_status_rejects_future_observation_before_update(client) -> None:
    response = client.post(
        "/v1/activity/devices/future-status/status",
        json={
            "platform": "android",
            "capability": "aggregate",
            "permission_status": "revoked",
            "status_observed_at": "2100-01-01T00:00:00Z",
            "collection_generation": 1,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_future_data"
    state = client.get(
        "/v1/activity/devices/future-status/collection"
    ).json()
    assert state["permission_status"] == "unknown"
    assert state["status_observed_at"] is None


def test_android_permission_status_requires_observation_and_generation(client) -> None:
    missing_observation = client.post(
        "/v1/activity/devices/android-boundary/status",
        json={
            "platform": "android",
            "capability": "aggregate",
            "permission_status": "granted",
            "collection_generation": 1,
        },
    )
    missing_generation = client.post(
        "/v1/activity/devices/android-boundary/status",
        json={
            "platform": "android",
            "capability": "aggregate",
            "permission_status": "granted",
            "status_observed_at": "2026-08-01T12:00:00Z",
        },
    )
    missing_permission = client.post(
        "/v1/activity/devices/android-boundary/status",
        json={
            "platform": "android",
            "capability": "aggregate",
            "status_observed_at": "2026-08-01T12:00:00Z",
            "collection_generation": 1,
        },
    )

    assert missing_observation.status_code == 422
    assert (
        missing_observation.json()["error"]["code"]
        == "activity_status_boundary_required"
    )
    assert missing_generation.status_code == 422
    assert (
        missing_generation.json()["error"]["code"]
        == "activity_status_boundary_required"
    )
    assert missing_permission.status_code == 422
    assert (
        missing_permission.json()["error"]["code"]
        == "activity_status_boundary_required"
    )


def test_delayed_ios_status_cannot_override_newer_revocation(client) -> None:
    revoked = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-status-ordering",
            "timezone": "UTC",
            "capability": "aggregate",
            "permission_status": "revoked",
            "reason": "screen_time_revoked",
            "collected_at": "2026-08-01T12:00:00Z",
            "samples": [],
        },
    )
    delayed_grant = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-status-ordering",
            "timezone": "UTC",
            "capability": "aggregate",
            "permission_status": "granted",
            "collected_at": "2026-08-01T11:00:00Z",
            "samples": [],
        },
    )

    assert revoked.status_code == 200
    assert delayed_grant.status_code == 200
    state = client.get(
        "/v1/activity/devices/iphone-status-ordering/collection"
    ).json()
    assert state["permission_status"] == "revoked"
    assert state["effective_collecting"] is False
    assert state["blocked_reason"] == "permission_revoked"
    assert datetime.fromisoformat(
        state["status_observed_at"].replace("Z", "+00:00")
    ) == datetime(2026, 8, 1, 12, tzinfo=UTC)


def test_equal_time_grant_cannot_override_revocation(client) -> None:
    observed_at = "2026-08-01T12:00:00Z"
    revoked = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-status-tie",
            "timezone": "UTC",
            "capability": "aggregate",
            "permission_status": "revoked",
            "reason": "screen_time_revoked",
            "collected_at": observed_at,
            "samples": [],
        },
    )
    granted = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-status-tie",
            "timezone": "UTC",
            "capability": "aggregate",
            "permission_status": "granted",
            "collected_at": observed_at,
            "samples": [],
        },
    )

    assert revoked.status_code == 200
    assert granted.status_code == 200
    state = client.get(
        "/v1/activity/devices/iphone-status-tie/collection"
    ).json()
    assert state["permission_status"] == "revoked"
    assert state["effective_collecting"] is False


def test_partial_manual_delete_rebuilds_the_day_summary(client, session) -> None:
    created = client.post(
        "/v1/activity/events/batch",
        json={
            "source_provider": "api-test-collector",
            "source_device": "desktop-delete-test",
            "platform": "macos",
            "capability": "detailed",
            "timezone": "UTC",
            "collection_revision": 0,
            "records": [
                {
                    "kind": "app_interval",
                    "source_record_id": "keep",
                    "start_at": "2026-08-01T09:00:00Z",
                    "end_at": "2026-08-01T09:30:00Z",
                    "state": "active",
                    "app_id": "editor",
                },
                {
                    "kind": "app_interval",
                    "source_record_id": "delete",
                    "start_at": "2026-08-01T10:00:00Z",
                    "end_at": "2026-08-01T10:30:00Z",
                    "state": "active",
                    "app_id": "browser",
                },
            ],
        },
    )
    assert created.status_code == 200

    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": "desktop-delete-test",
            "start": "2026-08-01T10:15:00Z",
            "end": "2026-08-01T10:20:00Z",
            "include_summaries": True,
            "include_control": False,
            "confirm": True,
        },
    )
    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-01", "timezone": "UTC"},
    )

    assert deleted.status_code == 200
    assert deleted.json()["raw_events_deleted"] == 1
    assert summary.json()["total_active_minutes"] == 55.0
    remaining = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_INTERVAL_EVENT))
    )
    assert sorted(row.payload["app_id"] for row in remaining) == [
        "browser",
        "browser",
        "editor",
    ]


def test_raw_only_manual_delete_cannot_leave_a_stale_summary(client) -> None:
    created = client.post(
        "/v1/activity/events/batch",
        json={
            "source_provider": "api-test-collector",
            "source_device": "desktop-raw-only-delete",
            "platform": "macos",
            "capability": "detailed",
            "timezone": "UTC",
            "collection_revision": 0,
            "records": [
                {
                    "kind": "app_interval",
                    "source_record_id": "keep",
                    "start_at": "2026-08-01T09:00:00Z",
                    "end_at": "2026-08-01T09:30:00Z",
                    "state": "active",
                    "app_id": "editor",
                },
                {
                    "kind": "app_interval",
                    "source_record_id": "delete",
                    "start_at": "2026-08-01T10:00:00Z",
                    "end_at": "2026-08-01T10:30:00Z",
                    "state": "active",
                    "app_id": "browser",
                },
            ],
        },
    )
    assert created.status_code == 200

    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": "desktop-raw-only-delete",
            "start": "2026-08-01T10:15:00Z",
            "end": "2026-08-01T10:20:00Z",
            "include_summaries": False,
            "include_control": False,
            "confirm": True,
        },
    )
    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-01", "timezone": "UTC"},
    )

    assert deleted.status_code == 200
    assert deleted.json()["summary_events_deleted"] == 0
    assert summary.json()["total_active_minutes"] == 55.0


def test_activitywatch_import_rejects_half_open_explicit_range_before_network(client) -> None:
    response = client.post(
        "/v1/activity/activitywatch/import",
        json={
            "device_id": "mac-api-test",
            "platform": "macos",
            "timezone": "UTC",
            "start_at": "2026-08-01T10:00:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_activitywatch_import_rejects_future_range_as_client_error(
    client,
    monkeypatch,
) -> None:
    def fail_if_called(self):
        raise AssertionError("invalid explicit range must not contact ActivityWatch")

    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient.list_buckets",
        fail_if_called,
    )

    response = client.post(
        "/v1/activity/activitywatch/import",
        json={
            "device_id": "mac-future-api-test",
            "platform": "macos",
            "timezone": "UTC",
            "start_at": "2100-01-01T10:00:00Z",
            "end_at": "2100-01-01T11:00:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_activitywatch_range"


def test_activitywatch_malformed_upstream_json_returns_502(
    client,
    monkeypatch,
) -> None:
    def malformed_client(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"{")
        )
        return httpx.Client(
            base_url=self.base_url,
            transport=transport,
        )

    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient._client",
        malformed_client,
    )

    response = client.post(
        "/v1/activity/activitywatch/import",
        json={
            "device_id": "mac-malformed-json",
            "platform": "macos",
            "timezone": "UTC",
            "start_at": "2026-08-01T10:00:00Z",
            "end_at": "2026-08-01T11:00:00Z",
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "activitywatch_error"


def test_activitywatch_unrepresentable_event_returns_502(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient.list_buckets",
        lambda self: {
            "window": {"type": "currentwindow"},
        },
    )
    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient.get_events",
        lambda self, bucket_id, *, start, end: [
            {
                "id": 1,
                "timestamp": "2026-08-01T10:00:00Z",
                "duration": 10**10_000,
                "data": {"app": "Code"},
            }
        ],
    )

    response = client.post(
        "/v1/activity/activitywatch/import",
        json={
            "device_id": "mac-unrepresentable-event",
            "platform": "macos",
            "timezone": "UTC",
            "start_at": "2026-08-01T10:00:00Z",
            "end_at": "2026-08-01T11:00:00Z",
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "activitywatch_error"


def test_generic_ingest_cannot_bypass_ios_detailed_capability_boundary(client) -> None:
    response = client.post(
        "/v1/activity/events/batch",
        json={
            "source_provider": "ios-bypass-attempt",
            "source_device": "iphone-bypass",
            "platform": "ios",
            "capability": "detailed",
            "timezone": "UTC",
            "records": [
                {
                    "kind": "app_interval",
                    "source_record_id": "private-app-timeline",
                    "start_at": "2026-08-01T10:00:00Z",
                    "end_at": "2026-08-01T11:00:00Z",
                    "state": "active",
                    "app_id": "private.app",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_targeted_delete_returns_409_before_creating_a_tombstone_when_raw_expired(
    client,
    session,
) -> None:
    created = client.post(
        "/v1/activity/events/batch",
        json={
            "source_provider": "api-expired-delete",
            "source_device": "desktop-expired-delete",
            "platform": "macos",
            "capability": "detailed",
            "timezone": "UTC",
            "collection_revision": 0,
            "records": [
                {
                    "kind": "app_interval",
                    "source_record_id": "expired",
                    "start_at": "2026-08-01T10:00:00Z",
                    "end_at": "2026-08-01T11:00:00Z",
                    "state": "active",
                    "app_id": "editor",
                }
            ],
        },
    )
    assert created.status_code == 200
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_INTERVAL_EVENT
        )
    )
    assert raw is not None
    raw.expires_at = datetime(2026, 8, 2, tzinfo=UTC)
    session.commit()

    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": "desktop-expired-delete",
            "start": "2026-08-01T10:15:00Z",
            "end": "2026-08-01T10:30:00Z",
            "include_summaries": True,
            "include_control": False,
            "confirm": True,
        },
    )

    assert deleted.status_code == 409
    assert (
        deleted.json()["error"]["code"]
        == "activity_deletion_requires_complete_raw"
    )
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DELETION_TOMBSTONE_EVENT
            )
        )
        is None
    )


def test_expired_daily_summary_is_hidden_before_maintenance(client, session) -> None:
    created = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="expired-summary",
                    app_id="editor.app",
                    seconds=1800,
                )
            ]
        ),
    )
    assert created.status_code == 200
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert daily is not None
    daily.expires_at = datetime(2026, 8, 2, tzinfo=UTC)
    session.commit()

    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-01", "timezone": "UTC"},
    )

    assert summary.status_code == 200
    assert summary.json()["status"] == "insufficient_data"
    assert summary.json()["reason"] == "no_activity_summary"


def test_maintenance_ignores_caller_supplied_future_clock(client, session) -> None:
    created = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="future-clock-protected",
                    app_id="editor.app",
                    seconds=1800,
                )
            ]
        ),
    )
    assert created.status_code == 200
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    )
    assert raw is not None
    raw.expires_at = datetime.now(UTC) + timedelta(days=1)
    session.commit()

    maintained = client.post(
        "/v1/activity/maintenance",
        params={"now": "2100-01-01T00:00:00Z"},
    )

    session.expire_all()
    assert maintained.status_code == 200
    assert maintained.json()["expired_events_deleted"] == 0
    assert session.get(WellnessEvent, raw.id) is not None


def test_wellness_context_rest_drops_unregistered_wearable_fields(
    client,
    monkeypatch,
) -> None:
    from healthmes.mcp_server import server as server_module

    async def malicious_readiness(date: str | None = None) -> dict:
        return {
            "status": "ok",
            "date": date,
            "raw_timeseries": [{"secret": "rest-raw-secret"}],
            "sleep_debt": {
                "status": "ok",
                "index": 20.0,
                "last_night": {
                    "date": date,
                    "score": 80.0,
                    "raw_sample": "rest-nested-secret",
                },
                "raw_timeseries": [1, 2, 3],
            },
            "charge": {
                "status": "ok",
                "entries": [
                    {
                        "category": "body_battery",
                        "provider": "garmin",
                        "value": 60.0,
                        "raw_payload": "rest-entry-secret",
                    }
                ],
            },
            "limitations": [],
        }

    monkeypatch.setattr(
        server_module,
        "get_daily_readiness_context",
        malicious_readiness,
    )

    response = client.post(
        "/v1/wellness-context/resolve",
        json={
            "question_kind": "focus",
            "date": "2026-08-01",
            "start": "2026-08-01T09:00:00Z",
            "end": "2026-08-01T10:00:00Z",
            "timezone": "UTC",
        },
    )

    assert response.status_code == 200
    wearable = response.json()["contexts"]["wearable"]
    assert wearable["sleep_debt"]["index"] == 20.0
    assert wearable["charge"]["entries"][0]["value"] == 60.0
    serialized = json.dumps(wearable)
    assert "raw_timeseries" not in serialized
    assert "raw_sample" not in serialized
    assert "raw_payload" not in serialized
    assert "rest-raw-secret" not in serialized


def test_wellness_context_rest_supports_fixed_offset_caffeine_day(client) -> None:
    response = client.post(
        "/v1/wellness-context/resolve",
        json={
            "question_kind": "caffeine_for_focus",
            "date": "2026-08-01",
            "start": "2026-08-01T01:00:00Z",
            "end": "2026-08-01T02:00:00Z",
            "timezone": "UTC+09:00",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["timezone"] == "UTC+09:00"
    assert result["contexts"]["nutrition"]["kind"] == "confirmed_caffeine_ledger"
    assert result["contexts"]["nutrition"]["context"]["local_date"] == "2026-08-01"
    assert result["contexts"]["nutrition"]["context"]["timezone"] == "UTC+09:00"
