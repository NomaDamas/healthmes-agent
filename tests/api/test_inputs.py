import os
import threading
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from healthmes.activity.locking import lock_activity_write_plane
from healthmes.activity.repository import COLLECTION_CONFIG_EVENT
from healthmes.inputs import (
    InputSettingsUpdate,
    InputSourceRegistry,
    InputSourceRegistryError,
)
from healthmes.storage import update_retention_policy
from healthmes.store import (
    Base,
    RawIngestEvent,
    RetentionPolicy,
    WellnessEvent,
    create_db_engine,
)

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


def _put_settings(
    client,
    source_id: str,
    body: dict,
    *,
    revision: str | None = None,
    quoted: bool = True,
):
    if revision is None:
        current = client.get(f"/v1/inputs/{source_id}")
        assert current.status_code == 200
        revision = current.json()["revision"]
    if_match = f'"{revision}"' if quoted else revision
    return client.put(
        f"/v1/inputs/{source_id}/settings",
        headers={"If-Match": if_match},
        json=body,
    )


def _resolve_openapi_schema(
    document: dict,
    schema: dict,
) -> dict:
    reference = schema.get("$ref")
    if reference is None:
        return schema
    prefix = "#/components/schemas/"
    assert reference.startswith(prefix)
    return document["components"]["schemas"][
        reference.removeprefix(prefix)
    ]


def _assert_error_matches_openapi(
    client,
    response,
) -> None:
    document = client.get("/openapi.json").json()
    operation = document["paths"][
        "/v1/inputs/{source_id}/settings"
    ]["put"]
    schema = operation["responses"][str(response.status_code)]["content"][
        "application/json"
    ]["schema"]
    validation_root = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": schema["$ref"],
        "components": document["components"],
    }
    errors = sorted(
        Draft202012Validator(validation_root).iter_errors(
            response.json()
        ),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    assert errors == []


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
    assert ios["actions"] == [
        {
            "action": "authorize",
            "execution": "device",
            "method": None,
            "endpoint": None,
            "requires_instance": True,
            "description": (
                "Unavailable in normal repository builds. An eligible, "
                "signed, entitlement-approved iPhone build may request "
                "Apple's App & Website Usage data authorization."
            ),
        },
        {
            "action": "sync",
            "execution": "device",
            "method": None,
            "endpoint": None,
            "requires_instance": True,
            "description": (
                "Unavailable in normal repository builds. After user "
                "authorization, the wired foreground and best-effort "
                "background lifecycle may upload completed Screen Time "
                "hours as authoritative snapshots."
            ),
        },
    ]
    assert ios["limitations"] == [
        "ios_screen_time_normal_build_unavailable",
        "ios_screen_time_export_requires_ios_26_4",
        "ios_screen_time_export_requires_apple_entitlement",
        "ios_screen_time_export_requires_signed_provisioning",
        "ios_screen_time_export_requires_user_authorization",
        "ios_screen_time_export_customer_access_is_eu_limited",
        "ios_screen_time_not_real_device_verified",
    ]
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
    assert response.headers["ETag"] == f'"{response.json()["revision"]}"'
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


def test_input_settings_requires_a_well_formed_if_match(client) -> None:
    missing = client.put(
        "/v1/inputs/activity.android/settings",
        json={"retention": {"activity_raw": "1d"}},
    )

    assert missing.status_code == 428
    assert (
        missing.json()["error"]["code"]
        == "input_settings_revision_required"
    )
    _assert_error_matches_openapi(client, missing)

    malformed_values = (
        "",
        "*",
        "W/\"" + "sha256:" + ("0" * 64) + "\"",
        "sha256:" + ("A" * 64),
        "sha256:" + ("0" * 63),
        "sha256:" + ("0" * 64) + ", " + "sha256:" + ("1" * 64),
        "\"sha256:" + ("0" * 64),
        "sha256:" + ("0" * 64) + "\"",
    )
    for value in malformed_values:
        response = client.put(
            "/v1/inputs/activity.android/settings",
            headers={"If-Match": value},
            json={"retention": {"activity_raw": "1d"}},
        )

        assert response.status_code == 400
        assert (
            response.json()["error"]["code"]
            == "input_settings_revision_invalid"
        )
        _assert_error_matches_openapi(client, response)

    duplicate = client.put(
        "/v1/inputs/activity.android/settings",
        headers=[
            ("If-Match", "sha256:" + ("0" * 64)),
            ("If-Match", "sha256:" + ("1" * 64)),
        ],
        json={"retention": {"activity_raw": "1d"}},
    )
    assert duplicate.status_code == 400
    assert (
        duplicate.json()["error"]["code"]
        == "input_settings_revision_invalid"
    )


def test_input_settings_accepts_unquoted_revision_and_noop_keeps_revision(
    client,
) -> None:
    before = client.get("/v1/inputs/activity.android").json()
    raw_preset = next(
        row["preset"]
        for row in before["retention"]
        if row["data_class"] == "activity_raw"
    )

    first = _put_settings(
        client,
        "activity.android",
        {"retention": {"activity_raw": raw_preset}},
        revision=before["revision"],
        quoted=False,
    )
    repeated = _put_settings(
        client,
        "activity.android",
        {"retention": {"activity_raw": raw_preset}},
        revision=before["revision"],
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["revision"] == before["revision"]
    assert repeated.json()["revision"] == before["revision"]
    assert first.headers["ETag"] == f'"{before["revision"]}"'


def test_unified_inputs_updates_ios_device_collection_settings(client) -> None:
    paused_until = datetime.now(UTC) + timedelta(hours=2)

    response = _put_settings(
        client,
        "activity.ios-screentime",
        {
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
    response = _put_settings(
        client,
        "activity.ios-screentime",
        {
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
    response = _put_settings(
        client,
        "activity.activitywatch",
        {
            "instance_id": "desktop-platform-input",
            "platform": "macOS",
            "enabled": True,
        },
    )

    assert response.status_code == 200
    instance = response.json()["instances"][0]
    assert instance["instance_id"] == "desktop-platform-input"
    assert instance["platform"] == "macos"

    conflict = _put_settings(
        client,
        "activity.activitywatch",
        {
            "instance_id": "desktop-platform-input",
            "platform": "linux",
            "enabled": False,
        },
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "input_platform_conflict"


def test_unified_inputs_rejects_platform_outside_source(client) -> None:
    response = _put_settings(
        client,
        "activity.ios-screentime",
        {
            "instance_id": "iphone-invalid-platform",
            "platform": "android",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input_platform_unsupported"


def test_unified_inputs_shares_domain_consent_and_activity_retention(client, session) -> None:
    response = _put_settings(
        client,
        "activity.ios-screentime",
        {
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
    nutrition = _put_settings(
        client,
        "nutrition.capture",
        {
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

    configured = _put_settings(
        client,
        "activity.android",
        {
            "instance_id": "shared-device-id",
            "enabled": True,
        },
    )
    assert configured.status_code == 200

    mismatch = _put_settings(
        client,
        "activity.ios-screentime",
        {
            "instance_id": "shared-device-id",
            "enabled": False,
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "input_instance_source_mismatch"


def test_unified_inputs_rejects_exclusions_for_non_activity_sources(
    client,
) -> None:
    response = _put_settings(
        client,
        "nutrition.capture",
        {
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
        response = _put_settings(
            client,
            "activity.ios-screentime",
            {
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

    disabled = _put_settings(
        client,
        "activity.ios-screentime",
        {
            "instance_id": device_id,
            "enabled": False,
        },
    )
    rejected = _put_settings(
        client,
        "activity.ios-screentime",
        {
            "instance_id": device_id,
            "enabled": True,
        },
    )

    assert disabled.status_code == 200
    assert disabled.json()["instances"][0]["enabled"] is False
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_ios_app_token"


def test_unified_inputs_rejects_unknown_retention_class(client) -> None:
    response = _put_settings(
        client,
        "activity.android",
        {"retention": {"nutrition_media": "7d"}},
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
        response = _put_settings(
            client,
            "activity.ios-screentime",
            body,
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

    updated = _put_settings(
        client,
        "activity.ios-screentime",
        {"retention": {"activity_raw": "1d"}},
        revision=before["revision"],
    )

    assert updated.status_code == 200
    assert updated.json()["revision"] != before["revision"]
    after = client.get("/v1/inputs/activity.ios-screentime")
    assert after.json()["revision"] == updated.json()["revision"]
    assert updated.headers["ETag"] == f'"{updated.json()["revision"]}"'
    assert after.headers["ETag"] == updated.headers["ETag"]


def test_stale_input_settings_update_changes_no_setting_group(client) -> None:
    paused_until = datetime.now(UTC) + timedelta(hours=4)
    before = client.get("/v1/inputs/activity.ios-screentime").json()
    accepted = _put_settings(
        client,
        "activity.ios-screentime",
        {
            "instance_id": "iphone-stale-settings",
            "enabled": True,
            "excluded_apps": [IOS_APP_A],
            "paused_until": paused_until.isoformat(),
            "decision_access_enabled": False,
            "retention": {
                "activity_raw": "1d",
                "activity_hourly": "30d",
                "activity_daily": "90d",
            },
        },
        revision=before["revision"],
    )
    assert accepted.status_code == 200

    stale = _put_settings(
        client,
        "activity.ios-screentime",
        {
            "instance_id": "iphone-stale-settings",
            "enabled": False,
            "excluded_apps": [IOS_APP_B],
            "paused_until": None,
            "decision_access_enabled": True,
            "retention": {
                "activity_raw": "7d",
                "activity_hourly": "7d",
                "activity_daily": "7d",
            },
        },
        revision=before["revision"],
    )

    assert stale.status_code == 409
    _assert_error_matches_openapi(client, stale)
    error = stale.json()["error"]
    assert error["code"] == "input_settings_revision_conflict"
    assert error["detail"] == {
        "expected_revision": before["revision"],
        "current_revision": accepted.json()["revision"],
    }
    current = client.get("/v1/inputs/activity.ios-screentime").json()
    assert current == accepted.json()


def test_input_settings_roll_back_every_group_after_partial_failure(
    client,
    monkeypatch,
) -> None:
    before = client.get("/v1/inputs/activity.ios-screentime").json()
    original_get = InputSourceRegistry._get_in_transaction
    calls = 0

    def fail_after_updates(self, session, source):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected post-update descriptor failure")
        return original_get(self, session, source)

    monkeypatch.setattr(
        InputSourceRegistry,
        "_get_in_transaction",
        fail_after_updates,
    )

    with pytest.raises(
        RuntimeError,
        match="injected post-update descriptor failure",
    ):
        _put_settings(
            client,
            "activity.ios-screentime",
            {
                "instance_id": "iphone-atomic-settings",
                "enabled": True,
                "excluded_apps": [IOS_APP_A],
                "decision_access_enabled": False,
                "retention": {
                    "activity_raw": "1d",
                    "activity_hourly": "30d",
                    "activity_daily": "90d",
                },
            },
            revision=before["revision"],
        )

    assert client.get(
        "/v1/inputs/activity.ios-screentime"
    ).json() == before


def test_concurrent_same_revision_updates_allow_one_sqlite_winner(
    settings,
    session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "healthmes.inputs.registry.activity_write_lock",
        nullcontext,
    )
    monkeypatch.setattr(
        "healthmes.storage.service.activity_write_lock",
        nullcontext,
    )
    registry = InputSourceRegistry(settings=settings)
    with session_factory() as session:
        revision = registry.get(
            session,
            "nutrition.capture",
        ).revision

    start = threading.Barrier(2)
    successes = []
    conflicts: list[InputSourceRegistryError] = []
    failures: list[BaseException] = []

    def update(preset: str) -> None:
        with session_factory() as session:
            try:
                start.wait(timeout=5)
                descriptor = InputSourceRegistry(
                    settings=settings
                ).update(
                    session,
                    "nutrition.capture",
                    InputSettingsUpdate(
                        retention={"nutrition_observation": preset}
                    ),
                    expected_revision=revision,
                )
            except InputSourceRegistryError as exc:
                session.rollback()
                conflicts.append(exc)
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                successes.append(descriptor)

    workers = [
        threading.Thread(target=update, args=(preset,))
        for preset in ("1d", "7d")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert failures == []
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "input_settings_revision_conflict"
    assert conflicts[0].detail["current_revision"] == successes[0].revision

    with session_factory() as session:
        current = registry.get(session, "nutrition.capture")
    assert current.revision == successes[0].revision
    raw_policy = next(
        row
        for row in current.retention
        if row.data_class == "nutrition_observation"
    )
    assert raw_policy.preset in {"1d", "7d"}


def test_sqlite_legacy_retention_writer_invalidates_input_revision(
    settings,
    session_factory,
    monkeypatch,
) -> None:
    legacy_has_fence = threading.Event()
    release_legacy = threading.Event()
    input_attempted_fence = threading.Event()

    def controlled_legacy_fence(session) -> None:
        lock_activity_write_plane(session)
        if session.info.get("legacy_retention_writer"):
            legacy_has_fence.set()
            if not release_legacy.wait(timeout=10):
                raise TimeoutError("test did not release legacy writer")

    def observed_input_fence(session) -> None:
        input_attempted_fence.set()
        lock_activity_write_plane(session)

    monkeypatch.setattr(
        "healthmes.inputs.registry.activity_write_lock",
        nullcontext,
    )
    monkeypatch.setattr(
        "healthmes.storage.service.activity_write_lock",
        nullcontext,
    )
    monkeypatch.setattr(
        "healthmes.storage.service.lock_activity_write_plane",
        controlled_legacy_fence,
    )
    monkeypatch.setattr(
        "healthmes.inputs.registry.lock_activity_write_plane",
        observed_input_fence,
    )

    registry = InputSourceRegistry(settings=settings)
    with session_factory() as session:
        revision = registry.get(
            session,
            "nutrition.capture",
        ).revision

    failures: list[BaseException] = []
    conflicts: list[InputSourceRegistryError] = []

    def legacy_update() -> None:
        with session_factory() as session:
            session.info["legacy_retention_writer"] = True
            try:
                update_retention_policy(
                    session,
                    "nutrition_observation",
                    "1d",
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    def input_update() -> None:
        with session_factory() as session:
            try:
                registry.update(
                    session,
                    "nutrition.capture",
                    InputSettingsUpdate(
                        retention={"nutrition_observation": "7d"}
                    ),
                    expected_revision=revision,
                )
            except InputSourceRegistryError as exc:
                session.rollback()
                conflicts.append(exc)
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    legacy_worker = threading.Thread(target=legacy_update)
    input_worker = threading.Thread(target=input_update)
    try:
        legacy_worker.start()
        assert legacy_has_fence.wait(timeout=10)
        input_worker.start()
        assert input_attempted_fence.wait(timeout=10)
        release_legacy.set()
        legacy_worker.join(timeout=10)
        input_worker.join(timeout=10)

        assert not legacy_worker.is_alive()
        assert not input_worker.is_alive()
        assert failures == []
        assert len(conflicts) == 1
        assert conflicts[0].code == "input_settings_revision_conflict"

        with session_factory() as session:
            current = registry.get(session, "nutrition.capture")
        policy = next(
            row
            for row in current.retention
            if row.data_class == "nutrition_observation"
        )
        assert policy.preset == "1d"
        assert conflicts[0].detail["current_revision"] == current.revision
    finally:
        release_legacy.set()
        if legacy_worker.ident is not None:
            legacy_worker.join(timeout=5)
        if input_worker.ident is not None:
            input_worker.join(timeout=5)


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_concurrent_same_revision_updates_use_postgres_write_fence(
    settings,
    monkeypatch,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(
        "healthmes.inputs.registry.activity_write_lock",
        nullcontext,
    )
    registry = InputSourceRegistry(settings=settings)
    with factory() as session:
        revision = registry.get(
            session,
            "activity.android",
        ).revision

    start = threading.Barrier(2)
    successes = []
    conflicts: list[InputSourceRegistryError] = []
    failures: list[BaseException] = []

    def update(preset: str) -> None:
        with factory() as session:
            try:
                start.wait(timeout=5)
                descriptor = InputSourceRegistry(
                    settings=settings
                ).update(
                    session,
                    "activity.android",
                    InputSettingsUpdate(
                        retention={"activity_raw": preset}
                    ),
                    expected_revision=revision,
                )
            except InputSourceRegistryError as exc:
                session.rollback()
                conflicts.append(exc)
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                successes.append(descriptor)

    workers = [
        threading.Thread(target=update, args=(preset,))
        for preset in ("1d", "7d")
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers)
        assert failures == []
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].code == "input_settings_revision_conflict"
        assert (
            conflicts[0].detail["current_revision"]
            == successes[0].revision
        )
    finally:
        for worker in workers:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_postgres_legacy_retention_writer_invalidates_input_revision(
    settings,
    monkeypatch,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )

    legacy_has_fence = threading.Event()
    release_legacy = threading.Event()
    input_attempted_fence = threading.Event()

    def controlled_legacy_fence(session) -> None:
        lock_activity_write_plane(session)
        if session.info.get("legacy_retention_writer"):
            legacy_has_fence.set()
            if not release_legacy.wait(timeout=10):
                raise TimeoutError("test did not release legacy writer")

    def observed_input_fence(session) -> None:
        input_attempted_fence.set()
        lock_activity_write_plane(session)

    monkeypatch.setattr(
        "healthmes.inputs.registry.activity_write_lock",
        nullcontext,
    )
    monkeypatch.setattr(
        "healthmes.storage.service.activity_write_lock",
        nullcontext,
    )
    monkeypatch.setattr(
        "healthmes.storage.service.lock_activity_write_plane",
        controlled_legacy_fence,
    )
    monkeypatch.setattr(
        "healthmes.inputs.registry.lock_activity_write_plane",
        observed_input_fence,
    )

    registry = InputSourceRegistry(settings=settings)
    with factory() as session:
        revision = registry.get(
            session,
            "nutrition.capture",
        ).revision

    failures: list[BaseException] = []
    conflicts: list[InputSourceRegistryError] = []

    def legacy_update() -> None:
        with factory() as session:
            session.info["legacy_retention_writer"] = True
            try:
                update_retention_policy(
                    session,
                    "nutrition_observation",
                    "1d",
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    def input_update() -> None:
        with factory() as session:
            try:
                registry.update(
                    session,
                    "nutrition.capture",
                    InputSettingsUpdate(
                        retention={"nutrition_observation": "7d"}
                    ),
                    expected_revision=revision,
                )
            except InputSourceRegistryError as exc:
                session.rollback()
                conflicts.append(exc)
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    legacy_worker = threading.Thread(target=legacy_update)
    input_worker = threading.Thread(target=input_update)
    try:
        legacy_worker.start()
        assert legacy_has_fence.wait(timeout=10)
        input_worker.start()
        assert input_attempted_fence.wait(timeout=10)
        release_legacy.set()
        legacy_worker.join(timeout=10)
        input_worker.join(timeout=10)

        assert not legacy_worker.is_alive()
        assert not input_worker.is_alive()
        assert failures == []
        assert len(conflicts) == 1
        assert conflicts[0].code == "input_settings_revision_conflict"

        with factory() as session:
            current = registry.get(session, "nutrition.capture")
        policy = next(
            row
            for row in current.retention
            if row.data_class == "nutrition_observation"
        )
        assert policy.preset == "1d"
        assert conflicts[0].detail["current_revision"] == current.revision
    finally:
        release_legacy.set()
        if legacy_worker.ident is not None:
            legacy_worker.join(timeout=5)
        if input_worker.ident is not None:
            input_worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


def test_input_settings_openapi_requires_if_match(client) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    operation = paths[
        "/v1/inputs/{source_id}/settings"
    ]["put"]
    parameters = [
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header"
    ]

    assert parameters == [
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "description": (
                "Current input descriptor revision from GET "
                "/v1/inputs/{source_id}. Accepts an exact sha256 tag in "
                "quoted or unquoted form."
            ),
            "schema": {
                "type": "string",
                "pattern": (
                    '^(?:"sha256:[0-9a-f]{64}"|'
                    "sha256:[0-9a-f]{64})$"
                ),
                "examples": [
                    "sha256:" + ("0" * 64),
                    '"sha256:' + ("0" * 64) + '"',
                ],
            },
        }
    ]
    assert operation["description"] == (
        "Atomically update input settings when If-Match equals the current "
        "descriptor revision. A semantically identical update returns 200 "
        "and preserves the revision, so that revision remains reusable."
    )
    assert {"400", "409", "428"} <= set(operation["responses"])

    expected_etag_header = {
        "description": (
            "Strong input descriptor revision. Send this exact value as "
            "If-Match on the next settings update."
        ),
        "schema": {
            "type": "string",
            "pattern": '^"sha256:[0-9a-f]{64}"$',
            "example": '"sha256:' + ("0" * 64) + '"',
        },
    }
    detail_get = paths["/v1/inputs/{source_id}"]["get"]
    assert detail_get["responses"]["200"]["headers"]["ETag"] == (
        expected_etag_header
    )
    assert operation["responses"]["200"]["headers"]["ETag"] == (
        expected_etag_header
    )

    def response_schema(status_code: str) -> dict:
        return _resolve_openapi_schema(
            schema,
            operation["responses"][status_code]["content"][
            "application/json"
            ]["schema"],
        )

    invalid = response_schema("400")
    required = response_schema("428")
    conflict = response_schema("409")
    invalid_error = _resolve_openapi_schema(
        schema,
        invalid["properties"]["error"],
    )
    required_error = _resolve_openapi_schema(
        schema,
        required["properties"]["error"],
    )
    conflict_error = _resolve_openapi_schema(
        schema,
        conflict["properties"]["error"],
    )
    assert invalid_error["properties"]["code"]["const"] == (
        "input_settings_revision_invalid"
    )
    assert required_error["properties"]["code"]["const"] == (
        "input_settings_revision_required"
    )
    assert conflict_error["properties"]["code"]["const"] == (
        "input_settings_revision_conflict"
    )
    conflict_detail = _resolve_openapi_schema(
        schema,
        conflict_error["properties"]["detail"],
    )
    assert conflict_detail["required"] == [
        "expected_revision",
        "current_revision",
    ]
    for name in conflict_detail["required"]:
        assert conflict_detail["properties"][name]["pattern"] == (
            "^sha256:[0-9a-f]{64}$"
        )


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

    response = _put_settings(
        client,
        "activity.android",
        {"retention": {"activity_raw": "1d"}},
    )

    assert response.status_code == 200
    session.expire_all()
    assert policy.enabled is True
    assert policy.retention_days == 1


def test_open_wearables_retention_isolated_from_generic_wellness(
    client,
    session,
) -> None:
    updated = _put_settings(
        client,
        "wearable.open-wearables",
        {"retention": {"wearable_normalized": "1d"}},
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
