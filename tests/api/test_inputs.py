from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from healthmes.activity.repository import COLLECTION_CONFIG_EVENT
from healthmes.store import RawIngestEvent, RetentionPolicy, WellnessEvent

IOS_KEY_FINGERPRINT = "1" * 40
IOS_APP_A = f"ios-app-v2-{IOS_KEY_FINGERPRINT}-" + ("a" * 40)
IOS_APP_B = f"ios-app-v2-{IOS_KEY_FINGERPRINT}-" + ("b" * 40)

EXPECTED_SOURCE_IDS = [
    "activity.android",
    "activity.activitywatch",
    "activity.ios-screentime",
    "nutrition.capture",
    "wearable.healthkit-bridge",
    "wearable.open-wearables",
    "calendar.google",
    "calendar.icloud",
]


def _source(payload: dict, source_id: str) -> dict:
    return next(
        item for item in payload["sources"] if item["source_id"] == source_id
    )


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


def test_unified_inputs_lists_stable_ui_contract(client) -> None:
    response = client.get("/v1/inputs")

    assert response.status_code == 200
    payload = response.json()
    assert [item["source_id"] for item in payload["sources"]] == EXPECTED_SOURCE_IDS

    ios = _source(payload, "activity.ios-screentime")
    assert ios["domain"] == "activity"
    assert ios["platforms"] == ["ios"]
    assert ios["capabilities"] == [
        "hourly_app_usage",
        "hourly_category_usage",
    ]
    assert ios["connection_state"] == "not_configured"
    assert ios["collection_state"] == "unavailable"
    assert ios["instances"] == []
    assert {
        setting["key"]: setting["scope"]
        for setting in ios["settings"]
    } == {
        "enabled": "instance",
        "excluded_apps": "instance",
        "paused_until": "instance",
        "decision_access_enabled": "domain",
        "retention": "data_class",
    }
    assert ios["privacy"]["raw_content_collected"] is False
    assert ios["privacy"]["default_llm_exposure"] == "aggregate_only"
    assert "ios_screen_time_export_requires_ios_26_4" in ios["limitations"]
    assert ios["revision"].startswith("sha256:")
    assert len(ios["revision"]) == 71

    google = _source(payload, "calendar.google")
    connect = next(
        action
        for action in google["actions"]
        if action["action"] == "connect"
    )
    assert connect["method"] == "POST"
    assert connect["endpoint"] == "/connect/google/start"

    healthkit = _source(payload, "wearable.healthkit-bridge")
    assert healthkit["connection_state"] == "configured"
    assert healthkit["collection_state"] == "idle"
    assert healthkit["retention"][0]["data_class"] == "raw_payload"
    assert healthkit["privacy"]["raw_content_collected"] is True
    assert healthkit["privacy"]["default_llm_exposure"] == "none"
    sync = next(
        action
        for action in healthkit["actions"]
        if action["action"] == "sync"
    )
    assert sync["endpoint"] == "/v1/ingest/healthkit"


def test_unified_inputs_returns_one_source_and_404s_unknown(client) -> None:
    response = client.get("/v1/inputs/nutrition.capture")

    assert response.status_code == 200
    assert response.json()["capabilities"] == [
        "photo_vlm",
        "free_text",
        "voice_transcript",
        "structured_nutrients",
        "caffeine",
    ]

    missing = client.get("/v1/inputs/not-a-source")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "input_source_not_found"


def test_unified_inputs_updates_ios_device_collection_settings(client) -> None:
    paused_until = datetime.now(UTC) + timedelta(hours=2)

    response = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={
            "instance_id": "iphone-input-settings",
            "enabled": True,
            "excluded_apps": [
                IOS_APP_A,
                f" {IOS_APP_A} ",
                IOS_APP_B,
            ],
            "paused_until": paused_until.isoformat(),
        },
    )

    assert response.status_code == 200
    descriptor = response.json()
    assert descriptor["connection_state"] == "configured"
    assert descriptor["collection_state"] == "paused"
    assert len(descriptor["instances"]) == 1
    instance = descriptor["instances"][0]
    assert instance["instance_id"] == "iphone-input-settings"
    assert instance["platform"] == "ios"
    assert instance["excluded_apps"] == [
        IOS_APP_A,
        IOS_APP_B,
    ]
    assert datetime.fromisoformat(instance["paused_until"]) == paused_until
    assert instance["config_revision"] == 1
    assert instance["status_observed_at"] is None


def test_unified_inputs_does_not_claim_config_only_device_is_collecting(
    client,
) -> None:
    response = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={
            "instance_id": "configured-only-iphone",
            "enabled": True,
        },
    )

    assert response.status_code == 200
    descriptor = response.json()
    assert descriptor["connection_state"] == "configured"
    assert descriptor["collection_state"] == "idle"
    assert descriptor["instances"][0]["effective_collecting"] is False
    assert descriptor["instances"][0]["status_observed_at"] is None


def test_unified_inputs_uses_and_persists_desktop_platform(client) -> None:
    response = client.put(
        "/v1/inputs/activity.activitywatch/settings",
        json={
            "instance_id": "desktop-platform-input",
            "platform": "macOS",
            "enabled": True,
        },
    )

    assert response.status_code == 200
    instance = response.json()["instances"][0]
    assert instance["instance_id"] == "desktop-platform-input"
    assert instance["platform"] == "macos"

    conflict = client.put(
        "/v1/inputs/activity.activitywatch/settings",
        json={
            "instance_id": "desktop-platform-input",
            "platform": "linux",
            "enabled": False,
        },
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "input_platform_conflict"


def test_unified_inputs_rejects_platform_outside_source(client) -> None:
    response = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={
            "instance_id": "iphone-invalid-platform",
            "platform": "android",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input_platform_unsupported"


def test_unified_inputs_shares_domain_consent_and_activity_retention(client, session) -> None:
    response = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={
            "decision_access_enabled": False,
            "retention": {
                "activity_raw": "1d",
                "activity_hourly": "30d",
                "activity_daily": "90d",
            },
        },
    )

    assert response.status_code == 200
    payload = client.get("/v1/inputs").json()
    for source_id in (
        "activity.android",
        "activity.activitywatch",
        "activity.ios-screentime",
    ):
        source = _source(payload, source_id)
        assert source["decision_access_enabled"] is False
        assert {
            row["data_class"]: row["preset"]
            for row in source["retention"]
        } == {
            "activity_raw": "1d",
            "activity_hourly": "30d",
            "activity_daily": "90d",
        }
        assert all(
            row["shared_across_source_instances"] is True
            for row in source["retention"]
        )

    policies = {
        row.data_class: row.retention_days
        for row in session.scalars(
            select(RetentionPolicy).where(
                RetentionPolicy.data_class.in_(
                    (
                        "activity_raw",
                        "activity_hourly",
                        "activity_daily",
                    )
                )
            )
        )
    }
    assert policies == {
        "activity_raw": 1,
        "activity_hourly": 30,
        "activity_daily": 90,
    }


def test_unified_inputs_rejects_unenforced_non_activity_collection_controls(
    client,
) -> None:
    nutrition = client.put(
        "/v1/inputs/nutrition.capture/settings",
        json={
            "instance_id": "phone",
            "platform": "ios",
            "enabled": False,
        },
    )
    assert nutrition.status_code == 422
    assert (
        nutrition.json()["error"]["code"]
        == "input_collection_settings_unsupported"
    )

    configured = client.put(
        "/v1/inputs/activity.android/settings",
        json={
            "instance_id": "shared-device-id",
            "enabled": True,
        },
    )
    assert configured.status_code == 200

    mismatch = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={
            "instance_id": "shared-device-id",
            "enabled": False,
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "input_instance_source_mismatch"


def test_unified_inputs_rejects_exclusions_for_non_activity_sources(
    client,
) -> None:
    response = client.put(
        "/v1/inputs/nutrition.capture/settings",
        json={
            "instance_id": "phone",
            "platform": "ios",
            "excluded_apps": ["com.example.private"],
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["code"]
        == "input_exclusions_unsupported"
    )


def test_unified_inputs_rejects_invalid_ios_exclusion_namespaces(client) -> None:
    invalid_sets = (
        ["com.example.private"],
        ["ios-app-" + ("a" * 40)],
        [
            IOS_APP_A,
            "ios-app-v2-" + ("2" * 40) + "-" + ("b" * 40),
        ],
    )

    for index, excluded_apps in enumerate(invalid_sets):
        response = client.put(
            "/v1/inputs/activity.ios-screentime/settings",
            json={
                "instance_id": f"iphone-invalid-exclusion-{index}",
                "excluded_apps": excluded_apps,
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_ios_app_token"


def test_unified_inputs_translates_legacy_ios_reenable_failure(
    client,
    session,
) -> None:
    device_id = "iphone-input-legacy-exclusion"
    _seed_legacy_ios_exclusion(client, session, device_id)

    disabled = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={
            "instance_id": device_id,
            "enabled": False,
        },
    )
    rejected = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={
            "instance_id": device_id,
            "enabled": True,
        },
    )

    assert disabled.status_code == 200
    assert disabled.json()["instances"][0]["enabled"] is False
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_ios_app_token"


def test_unified_inputs_rejects_unknown_retention_class(client) -> None:
    response = client.put(
        "/v1/inputs/activity.android/settings",
        json={"retention": {"nutrition_media": "7d"}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input_retention_class_unsupported"


def test_unified_inputs_rejects_empty_or_null_only_updates(client) -> None:
    for body in (
        {},
        {"retention": {}},
        {"instance_id": "iphone-noop"},
        {"enabled": None},
        {"decision_access_enabled": None},
    ):
        response = client.put(
            "/v1/inputs/activity.ios-screentime/settings",
            json=body,
        )

        assert response.status_code == 422


def test_unified_inputs_documents_source_specific_exclusion_ids(client) -> None:
    response = client.get("/v1/inputs/activity.ios-screentime")

    assert response.status_code == 200
    excluded_apps = next(
        setting
        for setting in response.json()["settings"]
        if setting["key"] == "excluded_apps"
    )
    assert "UsageStats package names" in excluded_apps["description"]
    assert "window event data.app values" in excluded_apps["description"]
    assert "device-keyed ios-app-v2-* tokens" in excluded_apps["description"]


def test_unified_inputs_revision_changes_for_retention_updates(client) -> None:
    before = client.get("/v1/inputs/activity.ios-screentime").json()

    updated = client.put(
        "/v1/inputs/activity.ios-screentime/settings",
        json={"retention": {"activity_raw": "1d"}},
    )

    assert updated.status_code == 200
    assert updated.json()["revision"] != before["revision"]


def test_retention_preset_reenables_a_disabled_policy(
    client,
    session,
) -> None:
    client.get("/v1/inputs/activity.android")
    policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "activity_raw"
        )
    )
    assert policy is not None
    policy.enabled = False
    policy.retention_days = 1
    session.commit()

    response = client.put(
        "/v1/inputs/activity.android/settings",
        json={"retention": {"activity_raw": "1d"}},
    )

    assert response.status_code == 200
    session.expire_all()
    assert policy.enabled is True
    assert policy.retention_days == 1


def test_open_wearables_retention_isolated_from_generic_wellness(
    client,
    session,
) -> None:
    updated = client.put(
        "/v1/inputs/wearable.open-wearables/settings",
        json={"retention": {"wearable_normalized": "1d"}},
    )

    assert updated.status_code == 200
    assert updated.json()["retention"] == [
        {
            "data_class": "wearable_normalized",
            "preset": "1d",
            "retention_days": 1,
            "enabled": True,
            "effective_preset": "1d",
            "shared_across_source_instances": True,
        }
    ]
    policies = {
        row.data_class: row.retention_days
        for row in session.scalars(select(RetentionPolicy))
    }
    assert policies["wearable_normalized"] == 1
    assert policies["normalized"] == 30


def test_healthkit_bridge_becomes_connected_after_first_raw_ingest(
    client,
    session,
) -> None:
    before = client.get("/v1/inputs/wearable.healthkit-bridge")
    assert before.status_code == 200
    assert before.json()["connection_state"] == "configured"

    session.add(
        RawIngestEvent(
            received_at=datetime.now(UTC),
            source="healthkit-bridge",
            content_type="application/json",
            path="raw_ingest/healthkit-test.json",
            size_bytes=2,
            sha256="a" * 64,
            parse_status="parsed",
            forward_status="nothing_mapped",
            forward_detail=None,
            records_forwarded=0,
        )
    )
    session.commit()

    after = client.get("/v1/inputs/wearable.healthkit-bridge")
    assert after.status_code == 200
    assert after.json()["connection_state"] == "connected"
