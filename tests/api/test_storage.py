import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import healthmes.nutrition.intake_service as intake_service_module
import healthmes.storage.service as storage_service
from healthmes.nutrition.confirmation_gate import (
    CONFIRMATION_EVENT,
    CONFIRMATION_PROVIDER,
    CONFIRMATION_SCHEMA,
    CONFIRMATION_TOMBSTONE_EVENT,
    NutritionConfirmationError,
    prepare_nutrition_confirmation,
)
from healthmes.nutrition.intake_contracts import (
    IntakeOutcome,
    IntakeOutcomeStatus,
)
from healthmes.nutrition.intake_service import (
    persist_outcome,
    terminal_outcome_status,
)
from healthmes.nutrition.operation_integrity import result_payload_digest
from healthmes.nutrition.repository import latest_interaction_transitions
from healthmes.storage import (
    register_storage_object,
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.store import (
    PurgeJob,
    RetentionPolicy,
    StorageObject,
    WellnessEvent,
)


def test_storage_settings_bootstraps_defaults_and_measures_files(
    client: TestClient, settings
) -> None:
    raw = settings.data_dir / "raw_ingest" / "2026" / "08" / "05" / "sample.json"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"12345")

    response = client.get("/v1/storage/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["data_dir"] == str(settings.data_dir.resolve())
    assert body["usage"]["raw_payload"] == {"bytes": 5, "objects": 1}
    policies = {row["data_class"]: row["preset"] for row in body["policies"]}
    assert policies["raw_payload"] == "14d"
    assert policies["media"] == "7d"
    assert policies["aggregate"] == "forever"
    assert body["backup"]["provider"] == "local"
    assert body["backup"]["snapshot_count"] == 0


def test_retention_update_is_persisted(client: TestClient, session) -> None:
    response = client.put("/v1/storage/settings/raw_payload", json={"preset": "1d"})
    assert response.status_code == 200
    assert response.json()["retention_days"] == 1

    session.expire_all()
    policy = session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.data_class == "raw_payload")
    )
    assert policy is not None
    assert policy.retention_days == 1


def test_retention_update_serializes_with_storage_object_writer(
    session,
    session_factory,
    settings,
) -> None:
    update_retention_policy(session, "media", "7d")
    session.commit()
    observed_at = datetime.now(UTC).replace(microsecond=0)
    writer_ready = threading.Event()
    release_writer = threading.Event()
    updater_started = threading.Event()
    updater_finished = threading.Event()
    writer_errors: list[BaseException] = []
    updater_errors: list[BaseException] = []
    object_ids: list[uuid.UUID] = []

    def write_object() -> None:
        try:
            with session_factory() as writer:
                obj = register_storage_object(
                    writer,
                    settings,
                    relative_path="media/retention-race.jpg",
                    data_class="media",
                    content_type="image/jpeg",
                    size_bytes=10,
                    observed_at=observed_at,
                )
                object_ids.append(obj.id)
                writer_ready.set()
                assert release_writer.wait(timeout=5)
                writer.commit()
        except BaseException as exc:
            writer_errors.append(exc)

    def update_policy() -> None:
        assert writer_ready.wait(timeout=5)
        updater_started.set()
        try:
            with session_factory() as updater:
                update_retention_policy(updater, "media", "1d")
                updater.commit()
        except BaseException as exc:
            updater_errors.append(exc)
        finally:
            updater_finished.set()

    writer = threading.Thread(target=write_object)
    updater = threading.Thread(target=update_policy)
    writer.start()
    updater.start()
    assert writer_ready.wait(timeout=5)
    assert updater_started.wait(timeout=5)
    assert not updater_finished.wait(timeout=0.2)
    release_writer.set()
    writer.join(timeout=5)
    updater.join(timeout=5)

    assert not writer.is_alive()
    assert not updater.is_alive()
    assert writer_errors == []
    assert updater_errors == []
    assert len(object_ids) == 1
    session.expire_all()
    obj = session.get(StorageObject, object_ids[0])
    assert obj is not None
    assert obj.expires_at.replace(tzinfo=UTC) == (observed_at + timedelta(days=1))


def test_retention_update_uses_the_original_object_observation_time(
    client: TestClient,
    session,
    settings,
) -> None:
    observed = datetime(2026, 7, 1, tzinfo=UTC)
    obj = register_storage_object(
        session,
        settings,
        relative_path="media/private.m4a",
        data_class="nutrition_media",
        content_type="audio/m4a",
        size_bytes=7,
        observed_at=observed,
    )
    session.commit()

    response = client.put(
        "/v1/storage/settings/nutrition_media",
        json={"preset": "1d"},
    )

    assert response.status_code == 200
    session.expire_all()
    stored = session.get(StorageObject, obj.id)
    assert stored is not None
    assert stored.retention_basis_at.replace(tzinfo=UTC) == observed
    assert stored.expires_at.replace(tzinfo=UTC) == observed + timedelta(days=7)


def test_wellness_event_contract_sets_expiry_and_is_idempotent(client: TestClient, session) -> None:
    observed = datetime(2026, 8, 1, 9, tzinfo=UTC)
    payload = {
        "event_type": "subjective_energy",
        "observed_at": observed.isoformat(),
        "source_provider": "manual",
        "source_record_id": "energy-1",
        "data_class": "normalized",
        "payload": {"score": 4},
    }
    first = client.post("/v1/wellness-events", json=payload)
    second = client.post("/v1/wellness-events", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    expires = datetime.fromisoformat(first.json()["expires_at"]).replace(tzinfo=UTC)
    assert expires == observed + timedelta(days=30)
    assert len(list(session.scalars(select(WellnessEvent)))) == 1


def test_maintenance_quarantines_malformed_json_without_stopping_other_rows(
    session,
    settings,
) -> None:
    expired_at = datetime(2026, 8, 7, tzinfo=UTC)
    malformed_expires_at = datetime(2026, 8, 9, tzinfo=UTC)
    malformed = WellnessEvent(
        event_type="nutrition.interaction.v1",
        observed_at=expired_at,
        recorded_at=expired_at,
        source_provider="nutrition-interaction",
        source_record_id=str(uuid.uuid4()),
        capture_method="text",
        quality_flags=["legacy-invalid"],  # type: ignore[arg-type]
        retention_policy_id=None,
        expires_at=malformed_expires_at,
        payload=["legacy-invalid"],  # type: ignore[arg-type]
    )
    healthy = WellnessEvent(
        event_type="subjective_energy",
        observed_at=expired_at,
        recorded_at=expired_at,
        source_provider="manual",
        source_record_id=str(uuid.uuid4()),
        capture_method="manual",
        quality_flags=None,
        retention_policy_id=None,
        expires_at=expired_at,
        payload={"score": 3},
    )
    session.add_all((malformed, healthy))
    session.commit()
    malformed_id = malformed.id
    healthy_id = healthy.id

    report = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    retained = session.get(WellnessEvent, malformed_id)
    assert retained is not None
    assert retained.payload == ["legacy-invalid"]
    assert retained.quality_flags == {
        "maintenance_quarantine": "legacy_json_document_invalid",
        "malformed_json_fields": ["payload", "quality_flags"],
    }
    assert session.get(WellnessEvent, healthy_id) is None
    assert report.errors == ()


def test_maintenance_purges_expired_malformed_health_payload(
    session,
    settings,
) -> None:
    expired_at = datetime(2026, 8, 7, tzinfo=UTC)
    malformed = WellnessEvent(
        event_type="subjective_energy",
        observed_at=expired_at,
        recorded_at=expired_at,
        source_provider="manual",
        source_record_id=str(uuid.uuid4()),
        capture_method="manual",
        quality_flags=["legacy-invalid"],  # type: ignore[arg-type]
        retention_policy_id=None,
        expires_at=expired_at,
        payload={"private_note": "must not outlive retention"},
    )
    session.add(malformed)
    session.commit()
    malformed_id = malformed.id

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2027, 8, 8, tzinfo=UTC),
    )
    session.commit()

    assert session.get(WellnessEvent, malformed_id) is None


@pytest.mark.parametrize("noncanonical_source_record_id", [False, True])
def test_maintenance_tombstones_malformed_expired_confirmation(
    session,
    settings,
    noncanonical_source_record_id: bool,
) -> None:
    action_id = uuid.uuid4()
    expired_at = datetime(2026, 8, 7, tzinfo=UTC)
    result = {
        "status": "cancelled",
        "operation_id": str(action_id),
    }
    confirmation = WellnessEvent(
        event_type=CONFIRMATION_EVENT,
        schema_version=1,
        observed_at=expired_at,
        recorded_at=expired_at,
        source_provider=CONFIRMATION_PROVIDER,
        source_record_id=(
            str(action_id).upper() if noncanonical_source_record_id else str(action_id)
        ),
        capture_method="confirmation-gate",
        quality_flags=None,
        retention_policy_id=None,
        expires_at=expired_at,
        payload={
            "schema_version": CONFIRMATION_SCHEMA,
            "action_id": str(action_id),
            "action": "confirm_photo_caffeine_observation",
            "snapshot": {"arguments": {"caffeine_mg": 500}},
            "snapshot_sha256": "0" * 64,
            "summary": {},
            "reply_handle_digest": "a" * 64,
            "state": "resolved",
            "prepared_at": expired_at.isoformat(),
            "expires_at": expired_at.isoformat(),
            "result": result,
            "result_sha256": result_payload_digest(result),
        },
    )
    session.add(confirmation)
    session.commit()
    confirmation_id = confirmation.id

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    assert session.get(WellnessEvent, confirmation_id) is None
    tombstone = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == CONFIRMATION_TOMBSTONE_EVENT,
            WellnessEvent.source_record_id == str(action_id),
        )
    )
    assert tombstone is not None
    assert tombstone.payload["opaque_terminal"] is True
    assert set(tombstone.payload).isdisjoint({"snapshot", "summary", "caffeine_mg"})
    with pytest.raises(
        NutritionConfirmationError,
        match="terminal and cannot be reused",
    ):
        prepare_nutrition_confirmation(
            session,
            action_id=action_id,
            action="confirm_photo_caffeine_observation",
            snapshot={"arguments": {"caffeine_mg": 95}},
            summary={},
            handle_secret="fixture-secret",
            source="test",
            now=datetime(2026, 8, 8, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "corruption",
    (
        "result_payload",
        "marker_payload",
        "result_quality_flags",
        "transition_payload",
    ),
)
def test_maintenance_quarantines_malformed_nutrition_json_documents(
    session,
    settings,
    corruption,
) -> None:
    expired_at = datetime(2026, 8, 7, tzinfo=UTC)
    interaction_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    event_type = "nutrition.intake-outcome.v1"
    source_provider = "nutrition-intake-outcome"
    source_record_id = str(operation_id)
    payload: object = {
        "outcome_id": str(operation_id),
        "interaction_id": str(interaction_id),
        "status": "cancelled",
        "operation_fingerprint": "a" * 64,
    }
    quality_flags: object | None = None
    expires_at = expired_at
    malformed_field = "payload"
    if corruption == "marker_payload":
        event_type = "nutrition.operation.v1"
        source_provider = "nutrition-operation"
        source_record_id = f"intake-outcome:{operation_id}"
        expires_at = None
        payload = "legacy-invalid"
    elif corruption == "result_quality_flags":
        malformed_field = "quality_flags"
        quality_flags = "legacy-invalid"
        expires_at = datetime(2026, 8, 9, tzinfo=UTC)
    elif corruption == "transition_payload":
        event_type = "nutrition.interaction-transition.v1"
        source_provider = "nutrition-interaction-transition"
        source_record_id = f"{interaction_id}:1"
        expires_at = None
        payload = ["legacy-invalid"]
    else:
        payload = ["legacy-invalid"]
        expires_at = datetime(2026, 8, 9, tzinfo=UTC)

    malformed = WellnessEvent(
        event_type=event_type,
        observed_at=expired_at,
        recorded_at=expired_at,
        source_provider=source_provider,
        source_record_id=source_record_id,
        capture_method="manual",
        quality_flags=quality_flags,  # type: ignore[arg-type]
        retention_policy_id=None,
        expires_at=expires_at,
        payload=payload,  # type: ignore[arg-type]
    )
    healthy = WellnessEvent(
        event_type="subjective_energy",
        observed_at=expired_at,
        recorded_at=expired_at,
        source_provider="manual",
        source_record_id=str(uuid.uuid4()),
        capture_method="manual",
        quality_flags=None,
        retention_policy_id=None,
        expires_at=expired_at,
        payload={"score": 3},
    )
    session.add_all((malformed, healthy))
    session.commit()
    malformed_id = malformed.id
    healthy_id = healthy.id

    report = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    retained = session.get(WellnessEvent, malformed_id)
    assert retained is not None
    assert retained.quality_flags == {
        "maintenance_quarantine": "legacy_json_document_invalid",
        "malformed_json_fields": [malformed_field],
    }
    assert session.get(WellnessEvent, healthy_id) is None
    assert report.errors == ()


@pytest.mark.parametrize(
    ("event_type", "source_provider"),
    (
        ("subjective_energy", "nutrition-operation"),
        ("subjective_energy", "nutrition-outcome-raw"),
        ("subjective_energy", "nutrition-future-internal"),
        ("nutrition.interaction.v1", "manual"),
    ),
)
def test_generic_wellness_api_rejects_internal_nutrition_namespaces(
    client: TestClient,
    session,
    event_type: str,
    source_provider: str,
) -> None:
    response = client.post(
        "/v1/wellness-events",
        json={
            "event_type": event_type,
            "observed_at": datetime.now(UTC).isoformat(),
            "source_provider": source_provider,
            "source_record_id": "reserved-namespace-attempt",
            "data_class": "normalized",
            "payload": {"note": "forged internal payload"},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reserved_wellness_namespace"
    assert session.scalar(select(WellnessEvent)) is None


def test_generic_wellness_api_rejects_maintenance_quality_flag(
    client: TestClient,
    session,
) -> None:
    response = client.post(
        "/v1/wellness-events",
        json={
            "event_type": "subjective_energy",
            "observed_at": datetime.now(UTC).isoformat(),
            "source_provider": "manual",
            "source_record_id": "forged-quarantine-attempt",
            "data_class": "normalized",
            "quality_flags": {"maintenance_quarantine": "legacy_transition_metadata_unmigrated"},
            "payload": {"score": 4},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reserved_wellness_quality_flag"
    assert session.scalar(select(WellnessEvent)) is None


def test_maintenance_does_not_trust_forged_quarantine_flag(
    session,
    settings,
) -> None:
    expired_at = datetime(2026, 8, 7, tzinfo=UTC)
    event = WellnessEvent(
        event_type="subjective_energy",
        schema_version=1,
        observed_at=expired_at,
        recorded_at=expired_at,
        timezone="Asia/Seoul",
        source_provider="manual",
        source_device=None,
        source_record_id="legacy-forged-quarantine",
        capture_method="manual",
        quality_flags={"maintenance_quarantine": "legacy_transition_metadata_unmigrated"},
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=expired_at,
        payload={"score": 4},
        derived_from=None,
    )
    session.add(event)
    session.commit()
    event_id = event.id

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()

    assert session.get(WellnessEvent, event_id) is None


def test_maintenance_dry_run_then_deletes_expired_object(session, settings) -> None:
    target = settings.data_dir / "raw_ingest" / "old.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"expired")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/old.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=7,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()

    preview = run_storage_maintenance(
        session, settings, dry_run=True, now=datetime(2026, 8, 5, tzinfo=UTC)
    )
    session.commit()
    assert preview.candidates == 1
    assert preview.deleted == 0
    assert target.exists()

    result = run_storage_maintenance(session, settings, now=datetime(2026, 8, 5, tzinfo=UTC))
    session.commit()
    session.refresh(obj)
    assert result.deleted == 1
    assert result.bytes_reclaimed == 7
    assert not target.exists()
    assert obj.purged_at is not None
    assert len(list(session.scalars(select(PurgeJob)))) == 1


def test_maintenance_dry_run_is_database_mutation_free(session, settings) -> None:
    event = WellnessEvent(
        event_type="subjective_energy",
        observed_at=datetime(2026, 8, 7, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 7, tzinfo=UTC),
        source_provider="manual",
        source_record_id=str(uuid.uuid4()),
        capture_method="manual",
        quality_flags=["legacy-invalid"],  # type: ignore[arg-type]
        retention_policy_id=None,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
        payload={"score": 3},
    )
    session.add(event)
    session.commit()
    event_id = event.id
    jobs_before = len(list(session.scalars(select(PurgeJob))))

    report = run_storage_maintenance(
        session,
        settings,
        dry_run=True,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    stored = session.get(WellnessEvent, event_id)
    assert stored is not None
    assert stored.quality_flags == ["legacy-invalid"]
    assert report.dry_run is True
    assert len(list(session.scalars(select(PurgeJob)))) == jobs_before


def test_maintenance_backfills_all_legacy_nutrition_operation_markers(
    session,
    settings,
) -> None:
    interaction_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    specs = (
        (
            "nutrition.confirmation.v1",
            "user-confirmation",
            "caffeine-confirmation",
            "caffeine_confirmation",
            "confirmation_id",
            {},
        ),
        (
            "nutrition.review.v1",
            "user-nutrition-review",
            "nutrition-review",
            "nutrition_review",
            "review_id",
            {},
        ),
        (
            "nutrition.daily-confirmation.v1",
            "user-confirmation",
            "daily-confirmation",
            "daily_intake_confirmation",
            "confirmation_id",
            {},
        ),
        (
            "nutrition.interaction-review.v1",
            "nutrition-intake-review",
            "intake-review",
            "intake_interaction_review",
            "review_id",
            {
                "interaction_id": interaction_id,
                "status": "confirmed",
            },
        ),
        (
            "nutrition.intake-outcome.v1",
            "nutrition-intake-outcome",
            "intake-outcome",
            "intake_outcome",
            "outcome_id",
            {
                "interaction_id": interaction_id,
                "status": "cancelled",
            },
        ),
        (
            "nutrition.decision-request.v1",
            "nutrition-decision-request",
            "intake-decision-request",
            "intake_decision_request",
            "request_id",
            {
                "interaction_id": interaction_id,
                "scope": "daily_nutrition",
            },
        ),
        (
            "nutrition.decision.v1",
            "nutrition-decision",
            "intake-decision",
            "intake_decision",
            "decision_id",
            {
                "interaction_id": interaction_id,
                "request_id": request_id,
                "status": "insufficient_data",
            },
        ),
    )
    operation_ids: dict[str, str] = {}
    result_digests: dict[str, str] = {}
    expired_at = datetime(2026, 8, 7, tzinfo=UTC)
    for (
        event_type,
        source_provider,
        operation_prefix,
        _operation_kind,
        payload_operation_id_field,
        payload,
    ) in specs:
        operation_id = str(uuid.uuid4())
        operation_ids[operation_prefix] = operation_id
        result_payload = {
            payload_operation_id_field: operation_id,
            "operation_fingerprint": "a" * 64,
            **payload,
        }
        result_digests[operation_prefix] = result_payload_digest(result_payload)
        session.add(
            WellnessEvent(
                event_type=event_type,
                schema_version=1,
                observed_at=expired_at,
                recorded_at=expired_at,
                timezone="Asia/Seoul",
                source_provider=source_provider,
                source_device="legacy-fixture",
                source_record_id=operation_id,
                capture_method="manual",
                quality_flags=None,
                confidence=None,
                sensitivity="wellness",
                consent_scope="personal",
                retention_policy_id=None,
                expires_at=expired_at,
                payload=result_payload,
                derived_from=None,
            )
        )
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    remaining_result_types = set(
        session.scalars(
            select(WellnessEvent.event_type).where(
                WellnessEvent.event_type.in_(event_type for event_type, *_rest in specs)
            )
        )
    )
    assert remaining_result_types == set()
    markers = {
        marker.source_record_id: marker
        for marker in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.operation.v1",
                WellnessEvent.source_provider == "nutrition-operation",
            )
        )
    }
    for (
        _event_type,
        _source_provider,
        operation_prefix,
        operation_kind,
        _payload_operation_id_field,
        _payload,
    ) in specs:
        marker = markers[f"{operation_prefix}:{operation_ids[operation_prefix]}"]
        assert marker.expires_at is None
        assert marker.payload["operation_kind"] == operation_kind
        assert marker.payload["operation_fingerprint"] == "a" * 64
        assert marker.payload["result_payload_sha256"] == (result_digests[operation_prefix])
        assert marker.payload["legacy_backfill"] is True
        assert set(marker.payload) == {
            "operation_kind",
            "operation_id",
            "operation_fingerprint",
            "operation_state",
            "result_payload_sha256",
            "legacy_backfill",
        }

    transitions = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.source_provider == "nutrition-interaction-transition",
            )
        )
    )
    transition_states = {
        (
            transition.payload["mutation_kind"],
            transition.payload["mutation_status"],
        )
        for transition in transitions
    }
    assert ("review", "confirmed") in transition_states
    assert ("outcome", "cancelled") in transition_states


@pytest.mark.parametrize(
    (
        "event_type",
        "source_provider",
        "operation_prefix",
        "operation_kind",
        "payload_operation_id_field",
    ),
    (
        (
            "nutrition.confirmation.v1",
            "user-confirmation",
            "caffeine-confirmation",
            "caffeine_confirmation",
            "confirmation_id",
        ),
        (
            "nutrition.review.v1",
            "user-nutrition-review",
            "nutrition-review",
            "nutrition_review",
            "review_id",
        ),
        (
            "nutrition.daily-confirmation.v1",
            "user-confirmation",
            "daily-confirmation",
            "daily_intake_confirmation",
            "confirmation_id",
        ),
        (
            "nutrition.interaction-review.v1",
            "nutrition-intake-review",
            "intake-review",
            "intake_interaction_review",
            "review_id",
        ),
        (
            "nutrition.intake-outcome.v1",
            "nutrition-intake-outcome",
            "intake-outcome",
            "intake_outcome",
            "outcome_id",
        ),
        (
            "nutrition.decision-request.v1",
            "nutrition-decision-request",
            "intake-decision-request",
            "intake_decision_request",
            "request_id",
        ),
        (
            "nutrition.decision.v1",
            "nutrition-decision",
            "intake-decision",
            "intake_decision",
            "decision_id",
        ),
    ),
)
def test_maintenance_tombstones_and_purges_legacy_result_identity_mismatch(
    session,
    settings,
    event_type,
    source_provider,
    operation_prefix,
    operation_kind,
    payload_operation_id_field,
):
    source_operation_id = uuid.uuid4()
    payload_operation_id = uuid.uuid4()
    expired_at = datetime(2026, 8, 7, tzinfo=UTC)
    result = WellnessEvent(
        event_type=event_type,
        schema_version=1,
        observed_at=expired_at,
        recorded_at=expired_at,
        timezone="Asia/Seoul",
        source_provider=source_provider,
        source_device="legacy-fixture",
        source_record_id=str(source_operation_id),
        capture_method="manual",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=expired_at,
        payload={
            payload_operation_id_field: str(payload_operation_id),
            "operation_fingerprint": "a" * 64,
        },
        derived_from=None,
    )
    session.add(result)
    session.commit()
    result_id = result.id

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    assert session.get(WellnessEvent, result_id) is None
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_provider == "nutrition-operation",
            WellnessEvent.source_record_id == f"{operation_prefix}:{source_operation_id}",
        )
    )
    assert marker is not None
    assert marker.payload == {
        "operation_kind": operation_kind,
        "operation_id": str(source_operation_id),
        "operation_fingerprint": None,
        "operation_state": "invalidated",
        "legacy_quarantine": True,
    }


def test_maintenance_does_not_rewrite_current_decision_payload(
    session,
    settings,
):
    decision_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 8, 8, tzinfo=UTC)
    payload = {
        "decision_id": str(decision_id),
        "operation_fingerprint": "a" * 64,
        "request_id": str(uuid.uuid4()),
        "interaction_id": str(uuid.uuid4()),
        "scope": "caffeine_sleep",
        "status": "proposal",
        "decided_at": recorded_at.isoformat(),
        "source": "fixture",
        "summary": "bounded caffeine proposal",
        "evidence_event_ids": [],
        "limitations": [],
        "recommendation": {
            "maximum_additional_mg": 100,
            "note": "keep this structured recommendation note",
        },
        "schema_version": "intake-decision-v1",
    }
    decision = WellnessEvent(
        event_type="nutrition.decision.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-decision",
        source_device="fixture",
        source_record_id=str(decision_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload=payload,
        derived_from=None,
    )
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-decision:{decision_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_decision",
            "operation_id": str(decision_id),
            "operation_fingerprint": "a" * 64,
            "operation_state": "completed",
            "result_payload_sha256": result_payload_digest(payload),
        },
        derived_from=None,
    )
    session.add_all((decision, marker))
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=recorded_at + timedelta(minutes=1),
    )
    session.commit()
    session.refresh(decision)
    session.refresh(marker)

    assert decision.payload == payload
    assert decision.quality_flags is None
    assert marker.quality_flags is None
    assert marker.payload["result_payload_sha256"] == result_payload_digest(payload)


def test_operation_marker_insert_recovers_from_stale_unique_race(
    session,
    monkeypatch,
):
    operation_id = uuid.uuid4()
    marker_id = f"caffeine-confirmation:{operation_id}"
    existing = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime(2026, 8, 8, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 8, tzinfo=UTC),
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=marker_id,
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "caffeine_confirmation",
            "operation_id": str(operation_id),
            "operation_fingerprint": "a" * 64,
            "operation_state": "completed",
        },
        derived_from=None,
    )
    session.add(existing)
    session.commit()

    original_lookup = storage_service._event_by_source_identity
    missed_stale_snapshot = False

    def stale_once(
        target_session,
        *,
        source_provider,
        source_record_id,
    ):
        nonlocal missed_stale_snapshot
        if (
            not missed_stale_snapshot
            and source_provider == "nutrition-operation"
            and source_record_id == marker_id
        ):
            missed_stale_snapshot = True
            return None
        return original_lookup(
            target_session,
            source_provider=source_provider,
            source_record_id=source_record_id,
        )

    monkeypatch.setattr(
        storage_service,
        "_event_by_source_identity",
        stale_once,
    )
    duplicate = WellnessEvent(
        event_type=existing.event_type,
        schema_version=existing.schema_version,
        observed_at=existing.observed_at,
        recorded_at=existing.recorded_at,
        timezone=existing.timezone,
        source_provider=existing.source_provider,
        source_device=existing.source_device,
        source_record_id=existing.source_record_id,
        capture_method=existing.capture_method,
        quality_flags=existing.quality_flags,
        confidence=existing.confidence,
        sensitivity=existing.sensitivity,
        consent_scope=existing.consent_scope,
        retention_policy_id=existing.retention_policy_id,
        expires_at=existing.expires_at,
        payload=dict(existing.payload),
        derived_from=existing.derived_from,
    )

    stored = storage_service._persist_event_by_source_identity(
        session,
        duplicate,
    )

    assert stored.id == existing.id
    assert missed_stale_snapshot is True
    assert session.in_transaction() is True


def test_maintenance_scrubs_health_metadata_from_completed_marker(
    session,
    settings,
):
    operation_id = uuid.uuid4()
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime(2026, 8, 8, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 8, tzinfo=UTC),
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-decision:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_decision",
            "operation_id": str(operation_id),
            "operation_fingerprint": "b" * 64,
            "operation_state": "completed",
            "interaction_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "decision_scope": "medication_interaction",
            "decision_status": "unsupported",
        },
        derived_from=None,
    )
    session.add(marker)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, 1, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert marker.payload == {
        "operation_kind": "intake_decision",
        "operation_id": marker.payload["operation_id"],
        "operation_fingerprint": "b" * 64,
        "operation_state": "completed",
    }


def test_maintenance_backfills_transition_from_legacy_marker_only_state(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    review_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)
    markers = (
        WellnessEvent(
            event_type="nutrition.operation.v1",
            schema_version=1,
            observed_at=recorded_at,
            recorded_at=recorded_at,
            timezone="Asia/Seoul",
            source_provider="nutrition-operation",
            source_device=None,
            source_record_id=f"intake-review:{review_id}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "operation_kind": "intake_interaction_review",
                "operation_id": str(review_id),
                "operation_fingerprint": "c" * 64,
                "operation_state": "completed",
                "interaction_id": str(interaction_id),
                "review_status": "confirmed",
            },
            derived_from=None,
        ),
        WellnessEvent(
            event_type="nutrition.operation.v1",
            schema_version=1,
            observed_at=recorded_at + timedelta(minutes=1),
            recorded_at=recorded_at + timedelta(minutes=1),
            timezone="Asia/Seoul",
            source_provider="nutrition-operation",
            source_device=None,
            source_record_id=f"intake-outcome:{outcome_id}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "operation_kind": "intake_outcome",
                "operation_id": str(outcome_id),
                "operation_fingerprint": "d" * 64,
                "operation_state": "completed",
                "interaction_id": str(interaction_id),
                "outcome_status": "cancelled",
            },
            derived_from=None,
        ),
    )
    session.add_all(markers)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    review_transition = latest_interaction_transitions(
        session,
        mutation_kind="review",
        interaction_ids={interaction_id},
    )[interaction_id]
    outcome_transition = latest_interaction_transitions(
        session,
        mutation_kind="outcome",
        interaction_ids={interaction_id},
    )[interaction_id]
    assert review_transition.payload["operation_id"] == str(review_id)
    assert review_transition.payload["mutation_status"] == "confirmed"
    assert outcome_transition.payload["operation_id"] == str(outcome_id)
    assert outcome_transition.payload["mutation_status"] == "cancelled"
    assert terminal_outcome_status(session, interaction_id) is IntakeOutcomeStatus.CANCELLED
    for marker in markers:
        session.refresh(marker)
        assert set(marker.payload) == {
            "operation_kind",
            "operation_id",
            "operation_fingerprint",
            "operation_state",
        }


@pytest.mark.parametrize(
    (
        "operation_kind",
        "source_prefix",
        "status_field",
        "statuses",
    ),
    (
        (
            "intake_outcome",
            "intake-outcome",
            "outcome_status",
            ("not_consumed", "cancelled"),
        ),
        (
            "intake_interaction_review",
            "intake-review",
            "review_status",
            ("confirmed", "rejected"),
        ),
    ),
)
def test_maintenance_quarantines_same_kind_legacy_transition_timestamp_tie(
    session,
    settings,
    operation_kind,
    source_prefix,
    status_field,
    statuses,
) -> None:
    interaction_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)
    markers = []
    for status in statuses:
        operation_id = uuid.uuid4()
        markers.append(
            WellnessEvent(
                event_type="nutrition.operation.v1",
                schema_version=1,
                observed_at=recorded_at,
                recorded_at=recorded_at,
                timezone="Asia/Seoul",
                source_provider="nutrition-operation",
                source_device=None,
                source_record_id=f"{source_prefix}:{operation_id}",
                capture_method="system",
                quality_flags=None,
                confidence=None,
                sensitivity="wellness",
                consent_scope="personal",
                retention_policy_id=None,
                expires_at=None,
                payload={
                    "operation_kind": operation_kind,
                    "operation_id": str(operation_id),
                    "operation_fingerprint": "e" * 64,
                    "operation_state": "completed",
                    "interaction_id": str(interaction_id),
                    status_field: status,
                },
                derived_from=None,
                created_at=recorded_at,
            )
        )
    session.add_all(markers)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    assert (
        list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                    WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
                )
            )
        )
        == []
    )
    for marker in markers:
        retained = session.get(WellnessEvent, marker.id)
        assert retained is not None
        assert retained.quality_flags == {
            "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
        }


def test_maintenance_appends_legacy_marker_after_existing_transition_revision(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    review_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)
    transition = WellnessEvent(
        event_type="nutrition.interaction-transition.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-interaction-transition",
        source_device=None,
        source_record_id=f"{interaction_id}:1",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "interaction_id": str(interaction_id),
            "revision": 1,
            "mutation_kind": "review",
            "operation_id": str(review_id),
            "mutation_status": "confirmed",
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=recorded_at + timedelta(minutes=1),
        recorded_at=recorded_at + timedelta(minutes=1),
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-outcome:{outcome_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_outcome",
            "operation_id": str(outcome_id),
            "operation_fingerprint": "d" * 64,
            "operation_state": "completed",
            "interaction_id": str(interaction_id),
            "outcome_status": "cancelled",
        },
        derived_from=None,
    )
    session.add_all((transition, marker))
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    transitions = sorted(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.payload["interaction_id"].as_string() == str(interaction_id),
            )
        ),
        key=lambda event: event.payload["revision"],
    )
    assert [event.payload["revision"] for event in transitions] == [1, 2]
    assert transitions[1].payload["operation_id"] == str(outcome_id)
    assert transitions[1].payload["mutation_status"] == "cancelled"
    session.refresh(marker)
    assert marker.quality_flags is None
    assert set(marker.payload) == {
        "operation_kind",
        "operation_id",
        "operation_fingerprint",
        "operation_state",
    }


def test_maintenance_quarantines_then_recovers_legacy_transition_metadata(
    session,
    settings,
):
    operation_id = uuid.uuid4()
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime(2026, 8, 7, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 7, tzinfo=UTC),
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-outcome:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_outcome",
            "operation_id": str(operation_id),
            "operation_fingerprint": "e" * 64,
            "operation_state": "completed",
            "interaction_id": "invalid",
            "outcome_status": "consumed",
        },
        derived_from=None,
    )
    session.add(marker)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert marker.payload["interaction_id"] == "invalid"
    assert marker.payload["outcome_status"] == "consumed"
    assert marker.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }

    interaction_id = uuid.uuid4()
    marker.payload = {
        **marker.payload,
        "interaction_id": str(interaction_id),
        "outcome_status": "cancelled",
    }
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, 1, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert marker.quality_flags is None
    assert set(marker.payload) == {
        "operation_kind",
        "operation_id",
        "operation_fingerprint",
        "operation_state",
    }
    assert terminal_outcome_status(session, interaction_id) is IntakeOutcomeStatus.CANCELLED


@pytest.mark.parametrize(
    "corruption",
    (
        "operation_kind",
        "source_prefix",
        "operation_state",
        "payload_operation_id",
    ),
)
def test_maintenance_quarantines_malformed_legacy_transition_marker(
    session,
    settings,
    corruption,
):
    interaction_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    operation_kind = "intake_outcom" if corruption == "operation_kind" else "intake_outcome"
    operation_state = "complete" if corruption == "operation_state" else "completed"
    source_prefix = "intake-review" if corruption == "source_prefix" else "intake-outcome"
    payload_operation_id = (
        "malformed-operation-id" if corruption == "payload_operation_id" else str(operation_id)
    )
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime(2026, 8, 7, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 7, tzinfo=UTC),
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"{source_prefix}:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": operation_kind,
            "operation_id": payload_operation_id,
            "operation_fingerprint": "f" * 64,
            "operation_state": operation_state,
            "interaction_id": str(interaction_id),
            "outcome_status": "consumed",
        },
        derived_from=None,
    )
    session.add(marker)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert marker.payload["interaction_id"] == str(interaction_id)
    assert marker.payload["outcome_status"] == "consumed"
    assert marker.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }
    transitions = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.payload["interaction_id"].as_string() == str(interaction_id),
            )
        )
    )
    assert transitions == []


@pytest.mark.parametrize(
    "corruption",
    (
        "operation_kind",
        "operation_state",
        "payload_operation_id",
    ),
)
def test_maintenance_purges_expired_result_on_malformed_marker_collision(
    session,
    settings,
    corruption,
):
    interaction_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)
    result = WellnessEvent(
        event_type="nutrition.intake-outcome.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-intake-outcome",
        source_device="legacy-fixture",
        source_record_id=str(operation_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=recorded_at,
        payload={
            "outcome_id": str(operation_id),
            "interaction_id": str(interaction_id),
            "status": "cancelled",
            "operation_fingerprint": "a" * 64,
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-outcome:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": (
                "intake_interaction_review" if corruption == "operation_kind" else "intake_outcome"
            ),
            "operation_id": (
                str(uuid.uuid4()) if corruption == "payload_operation_id" else str(operation_id)
            ),
            "operation_fingerprint": "a" * 64,
            "operation_state": ("processing" if corruption == "operation_state" else "completed"),
        },
        derived_from=None,
    )
    session.add_all((result, marker))
    session.commit()
    result_id = result.id
    marker_id = marker.id

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    retained_result = session.get(WellnessEvent, result_id)
    retained_marker = session.get(WellnessEvent, marker_id)
    assert retained_result is None
    assert retained_marker is not None
    assert retained_marker.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }


def test_maintenance_rejects_noncanonical_transition_operation_uuid(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    operation_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)
    result = WellnessEvent(
        event_type="nutrition.intake-outcome.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-intake-outcome",
        source_device="legacy-fixture",
        source_record_id=str(operation_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=recorded_at,
        payload={
            "outcome_id": str(operation_id),
            "interaction_id": str(interaction_id),
            "status": "cancelled",
            "operation_fingerprint": "a" * 64,
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    transition = WellnessEvent(
        event_type="nutrition.interaction-transition.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-interaction-transition",
        source_device=None,
        source_record_id=f"{interaction_id}:1",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "interaction_id": str(interaction_id),
            "revision": 1,
            "mutation_kind": "outcome",
            "operation_id": str(operation_id).upper(),
            "mutation_status": "cancelled",
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    session.add_all((result, transition))
    session.commit()
    result_id = result.id

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    assert session.get(WellnessEvent, result_id) is None
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_record_id == f"intake-outcome:{operation_id}",
        )
    )
    assert marker is not None
    assert marker.payload == {
        "operation_kind": "intake_outcome",
        "operation_id": str(operation_id),
        "operation_fingerprint": None,
        "operation_state": "invalidated",
        "legacy_quarantine": True,
    }
    with pytest.raises(
        RuntimeError,
        match="invalid interaction transition chain",
    ):
        terminal_outcome_status(session, interaction_id)


def test_maintenance_does_not_backfill_review_after_outcome(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    review_id = uuid.uuid4()
    outcome_at = datetime(2026, 8, 7, 10, tzinfo=UTC)
    review_at = outcome_at + timedelta(minutes=1)
    outcome = WellnessEvent(
        event_type="nutrition.intake-outcome.v1",
        schema_version=1,
        observed_at=outcome_at,
        recorded_at=outcome_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-intake-outcome",
        source_device="legacy-fixture",
        source_record_id=str(outcome_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
        payload={
            "outcome_id": str(outcome_id),
            "interaction_id": str(interaction_id),
            "status": "not_consumed",
            "operation_fingerprint": "a" * 64,
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    review = WellnessEvent(
        event_type="nutrition.interaction-review.v1",
        schema_version=1,
        observed_at=review_at,
        recorded_at=review_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-intake-review",
        source_device="legacy-fixture",
        source_record_id=str(review_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
        payload={
            "review_id": str(review_id),
            "interaction_id": str(interaction_id),
            "status": "confirmed",
            "operation_fingerprint": "b" * 64,
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    session.add_all((outcome, review))
    session.commit()
    result_ids = (outcome.id, review.id)

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    retained = [session.get(WellnessEvent, result_id) for result_id in result_ids]
    assert all(event is not None for event in retained)
    assert all(
        event.quality_flags == {"maintenance_quarantine": "legacy_transition_metadata_unmigrated"}
        for event in retained
        if event is not None
    )
    transitions = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
            )
        )
    )
    assert transitions == []


def test_maintenance_uses_created_at_before_recorded_at_for_legacy_order(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    review_id = uuid.uuid4()
    created_at = datetime(2026, 8, 7, 10, tzinfo=UTC)

    def result_event(
        *,
        event_type: str,
        source_provider: str,
        source_record_id: uuid.UUID,
        payload: dict[str, str],
        created_offset: int,
        recorded_at: datetime,
    ) -> WellnessEvent:
        return WellnessEvent(
            event_type=event_type,
            schema_version=1,
            observed_at=recorded_at,
            recorded_at=recorded_at,
            timezone="Asia/Seoul",
            source_provider=source_provider,
            source_device="legacy-fixture",
            source_record_id=str(source_record_id),
            capture_method="manual",
            quality_flags=None,
            confidence=1.0,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=datetime(2026, 8, 9, tzinfo=UTC),
            payload=payload,
            derived_from={"interaction_id": str(interaction_id)},
            created_at=created_at + timedelta(minutes=created_offset),
        )

    outcome = result_event(
        event_type="nutrition.intake-outcome.v1",
        source_provider="nutrition-intake-outcome",
        source_record_id=outcome_id,
        payload={
            "outcome_id": str(outcome_id),
            "interaction_id": str(interaction_id),
            "status": "not_consumed",
            "operation_fingerprint": "a" * 64,
        },
        created_offset=0,
        recorded_at=created_at + timedelta(hours=1),
    )
    review = result_event(
        event_type="nutrition.interaction-review.v1",
        source_provider="nutrition-intake-review",
        source_record_id=review_id,
        payload={
            "review_id": str(review_id),
            "interaction_id": str(interaction_id),
            "status": "confirmed",
            "operation_fingerprint": "b" * 64,
        },
        created_offset=1,
        recorded_at=created_at - timedelta(hours=1),
    )
    session.add_all((outcome, review))
    session.commit()
    result_ids = (outcome.id, review.id)

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    retained = [session.get(WellnessEvent, result_id) for result_id in result_ids]
    assert all(event is not None for event in retained)
    assert all(
        event.quality_flags == {"maintenance_quarantine": "legacy_transition_metadata_unmigrated"}
        for event in retained
        if event is not None
    )
    assert (
        list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                    WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
                )
            )
        )
        == []
    )


def test_maintenance_keeps_marker_quarantined_for_invalid_transition_chain(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    review_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)

    def transition(
        revision: int,
        mutation_kind: str,
        operation_id: uuid.UUID,
        mutation_status: str,
    ) -> WellnessEvent:
        event_at = recorded_at + timedelta(minutes=revision)
        return WellnessEvent(
            event_type="nutrition.interaction-transition.v1",
            schema_version=1,
            observed_at=event_at,
            recorded_at=event_at,
            timezone="Asia/Seoul",
            source_provider="nutrition-interaction-transition",
            source_device=None,
            source_record_id=f"{interaction_id}:{revision}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "interaction_id": str(interaction_id),
                "revision": revision,
                "mutation_kind": mutation_kind,
                "operation_id": str(operation_id),
                "mutation_status": mutation_status,
            },
            derived_from={"interaction_id": str(interaction_id)},
        )

    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=recorded_at + timedelta(minutes=3),
        recorded_at=recorded_at + timedelta(minutes=3),
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-outcome:{outcome_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_outcome",
            "operation_id": str(outcome_id),
            "operation_fingerprint": "a" * 64,
            "operation_state": "completed",
            "interaction_id": str(interaction_id),
            "outcome_status": "cancelled",
        },
        derived_from=None,
    )
    session.add_all(
        (
            transition(1, "review", review_id, "confirmed"),
            transition(3, "outcome", outcome_id, "cancelled"),
            marker,
        )
    )
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert marker.payload["interaction_id"] == str(interaction_id)
    assert marker.payload["outcome_status"] == "cancelled"
    assert marker.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }
    revisions = sorted(
        session.scalars(
            select(WellnessEvent.payload["revision"].as_integer()).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.payload["interaction_id"].as_string() == str(interaction_id),
            )
        )
    )
    assert revisions == [1, 3]


def test_maintenance_keeps_result_until_transition_backfill_is_safe(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    review_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    conflicting_outcome_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)

    def transition(
        revision: int,
        operation_id: uuid.UUID,
        mutation_kind: str,
        mutation_status: str,
    ) -> WellnessEvent:
        event_at = recorded_at + timedelta(minutes=revision)
        return WellnessEvent(
            event_type="nutrition.interaction-transition.v1",
            schema_version=1,
            observed_at=event_at,
            recorded_at=event_at,
            timezone="Asia/Seoul",
            source_provider="nutrition-interaction-transition",
            source_device=None,
            source_record_id=f"{interaction_id}:{revision}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "interaction_id": str(interaction_id),
                "revision": revision,
                "mutation_kind": mutation_kind,
                "operation_id": str(operation_id),
                "mutation_status": mutation_status,
            },
            derived_from={"interaction_id": str(interaction_id)},
        )

    result = WellnessEvent(
        event_type="nutrition.intake-outcome.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-intake-outcome",
        source_device="legacy-fixture",
        source_record_id=str(outcome_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=datetime(2026, 8, 8, 0, 30, tzinfo=UTC),
        payload={
            "outcome_id": str(outcome_id),
            "interaction_id": str(interaction_id),
            "status": "cancelled",
            "operation_fingerprint": "a" * 64,
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    invalid_transition = transition(
        3,
        conflicting_outcome_id,
        "outcome",
        "not_consumed",
    )
    session.add_all(
        (
            transition(1, review_id, "review", "confirmed"),
            invalid_transition,
            result,
        )
    )
    session.commit()
    result_id = result.id

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    quarantined = session.get(WellnessEvent, result_id)
    assert quarantined is not None
    assert quarantined.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_record_id == f"intake-outcome:{outcome_id}",
        )
    )
    assert marker is None

    stored_invalid_transition = session.get(
        WellnessEvent,
        invalid_transition.id,
    )
    assert stored_invalid_transition is not None
    session.delete(stored_invalid_transition)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, 1, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    assert session.get(WellnessEvent, result_id) is None
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_record_id == f"intake-outcome:{outcome_id}",
        )
    )
    assert marker is not None
    transitions = list(
        session.scalars(
            select(WellnessEvent)
            .where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
            )
            .order_by(WellnessEvent.payload["revision"].as_integer())
        )
    )
    assert [transition.payload["revision"] for transition in transitions] == [1, 2]
    assert transitions[1].payload["operation_id"] == str(outcome_id)
    assert transitions[1].payload["mutation_status"] == "cancelled"


def test_maintenance_quarantine_is_stable_during_transition_only_repair(
    session,
    settings,
    monkeypatch,
):
    interaction_id = uuid.uuid4()
    review_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    conflicting_outcome_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)

    def transition(
        revision: int,
        operation_id: uuid.UUID,
        mutation_kind: str,
        mutation_status: str,
    ) -> WellnessEvent:
        event_at = recorded_at + timedelta(minutes=revision)
        return WellnessEvent(
            event_type="nutrition.interaction-transition.v1",
            schema_version=1,
            observed_at=event_at,
            recorded_at=event_at,
            timezone="Asia/Seoul",
            source_provider="nutrition-interaction-transition",
            source_device=None,
            source_record_id=f"{interaction_id}:{revision}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "interaction_id": str(interaction_id),
                "revision": revision,
                "mutation_kind": mutation_kind,
                "operation_id": str(operation_id),
                "mutation_status": mutation_status,
            },
            derived_from={"interaction_id": str(interaction_id)},
        )

    result = WellnessEvent(
        event_type="nutrition.intake-outcome.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider="nutrition-intake-outcome",
        source_device="legacy-fixture",
        source_record_id=str(outcome_id),
        capture_method="manual",
        quality_flags=None,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
        payload={
            "outcome_id": str(outcome_id),
            "interaction_id": str(interaction_id),
            "status": "cancelled",
            "operation_fingerprint": "a" * 64,
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    invalid_transition = transition(
        3,
        conflicting_outcome_id,
        "outcome",
        "not_consumed",
    )
    session.add_all(
        (
            transition(1, review_id, "review", "confirmed"),
            invalid_transition,
            result,
        )
    )
    session.commit()
    result_id = result.id
    invalid_transition_id = invalid_transition.id

    def repair_transition_only(_session, _settings):
        stored_invalid = _session.get(
            WellnessEvent,
            invalid_transition_id,
        )
        assert stored_invalid is not None
        _session.delete(stored_invalid)
        _session.flush()
        _session.add(transition(2, outcome_id, "outcome", "cancelled"))
        _session.flush()

    monkeypatch.setattr(
        storage_service,
        "_discover_unindexed",
        repair_transition_only,
    )

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    quarantined = session.get(WellnessEvent, result_id)
    assert quarantined is not None
    assert quarantined.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.operation.v1",
                WellnessEvent.source_record_id == f"intake-outcome:{outcome_id}",
            )
        )
        is None
    )


def test_maintenance_does_not_recover_explicit_null_marker_operation_id(
    session,
    settings,
):
    operation_id = uuid.uuid4()
    interaction_id = uuid.uuid4()
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime(2026, 8, 7, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 7, tzinfo=UTC),
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-outcome:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_outcome",
            "operation_id": None,
            "operation_fingerprint": "a" * 64,
            "operation_state": "completed",
            "interaction_id": str(interaction_id),
            "outcome_status": "cancelled",
        },
        derived_from=None,
    )
    session.add(marker)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert "operation_id" in marker.payload
    assert marker.payload["operation_id"] is None
    assert marker.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }
    assert (
        list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                    WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
                )
            )
        )
        == []
    )


def test_maintenance_quarantines_noncanonical_marker_source_uuid(
    session,
    settings,
):
    operation_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    interaction_id = uuid.uuid4()
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime(2026, 8, 7, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 7, tzinfo=UTC),
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-outcome:{str(operation_id).upper()}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_outcome",
            "operation_id": str(operation_id),
            "operation_fingerprint": "a" * 64,
            "operation_state": "completed",
            "interaction_id": str(interaction_id),
            "outcome_status": "cancelled",
        },
        derived_from=None,
    )
    session.add(marker)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert marker.payload["interaction_id"] == str(interaction_id)
    assert marker.payload["outcome_status"] == "cancelled"
    assert marker.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }
    transitions = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
            )
        )
    )
    assert transitions == []


def test_maintenance_rejects_transition_source_namespace_collision(
    session,
    settings,
):
    interaction_id = uuid.uuid4()
    conflicting_interaction_id = uuid.uuid4()
    review_id = uuid.uuid4()
    conflicting_outcome_id = uuid.uuid4()
    marker_outcome_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 7, tzinfo=UTC)

    def transition(
        revision: int,
        payload_interaction_id: uuid.UUID,
        operation_id: uuid.UUID,
        mutation_kind: str,
        mutation_status: str,
    ) -> WellnessEvent:
        event_at = recorded_at + timedelta(minutes=revision)
        return WellnessEvent(
            event_type="nutrition.interaction-transition.v1",
            schema_version=1,
            observed_at=event_at,
            recorded_at=event_at,
            timezone="Asia/Seoul",
            source_provider="nutrition-interaction-transition",
            source_device=None,
            source_record_id=f"{interaction_id}:{revision}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "interaction_id": str(payload_interaction_id),
                "revision": revision,
                "mutation_kind": mutation_kind,
                "operation_id": str(operation_id),
                "mutation_status": mutation_status,
            },
            derived_from={"interaction_id": str(payload_interaction_id)},
        )

    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=recorded_at + timedelta(minutes=3),
        recorded_at=recorded_at + timedelta(minutes=3),
        timezone="Asia/Seoul",
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-outcome:{marker_outcome_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_outcome",
            "operation_id": str(marker_outcome_id),
            "operation_fingerprint": "a" * 64,
            "operation_state": "completed",
            "interaction_id": str(interaction_id),
            "outcome_status": "cancelled",
        },
        derived_from=None,
    )
    session.add_all(
        (
            transition(
                1,
                interaction_id,
                review_id,
                "review",
                "confirmed",
            ),
            transition(
                2,
                conflicting_interaction_id,
                conflicting_outcome_id,
                "outcome",
                "cancelled",
            ),
            marker,
        )
    )
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.refresh(marker)

    assert marker.payload["interaction_id"] == str(interaction_id)
    assert marker.payload["outcome_status"] == "cancelled"
    assert marker.quality_flags == {
        "maintenance_quarantine": "legacy_transition_metadata_unmigrated"
    }
    source_namespace_rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
            )
        )
    )
    assert len(source_namespace_rows) == 2


def test_maintenance_and_runtime_transition_write_share_ledger_lock(
    client,
    session,
    session_factory,
    settings,
    monkeypatch,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "observed_at": "2026-08-08T12:30:00+09:00",
            "timezone": "Asia/Seoul",
            "source": "ios-device",
            "source_text": "이 음료를 마셔도 될까?",
            "items": [],
        },
    )
    assert interaction.status_code == 201
    interaction_id = uuid.UUID(interaction.json()["interaction_id"])
    writer_attempted = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []
    writer: threading.Thread | None = None
    runtime_lock = intake_service_module.lock_nutrition_ledger

    def observe_runtime_lock(writer_session):
        writer_attempted.set()
        runtime_lock(writer_session)

    def write_outcome() -> None:
        try:
            with session_factory() as writer_session:
                persist_outcome(
                    writer_session,
                    IntakeOutcome(
                        outcome_id=uuid.uuid4(),
                        operation_fingerprint="a" * 64,
                        interaction_id=interaction_id,
                        status=IntakeOutcomeStatus.NOT_CONSUMED,
                        confirmed_at=datetime.now(UTC),
                        source="fixture-user",
                    ),
                )
                writer_session.commit()
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    def start_runtime_writer(_session, _settings) -> None:
        nonlocal writer
        writer = threading.Thread(target=write_outcome)
        writer.start()
        assert writer_attempted.wait(timeout=2)
        assert not writer_finished.wait(timeout=0.2)

    monkeypatch.setattr(
        intake_service_module,
        "lock_nutrition_ledger",
        observe_runtime_lock,
    )
    monkeypatch.setattr(
        storage_service,
        "_discover_unindexed",
        start_runtime_writer,
    )

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 8, 15, tzinfo=UTC),
    )
    assert writer is not None
    assert not writer_finished.is_set()
    session.commit()

    writer.join(timeout=5)
    assert not writer.is_alive()
    assert writer_finished.is_set()
    assert writer_errors == []
    with session_factory() as verification:
        assert (
            terminal_outcome_status(
                verification,
                interaction_id,
            )
            is IntakeOutcomeStatus.NOT_CONSUMED
        )


def test_storage_web_page_renders(client: TestClient) -> None:
    response = client.get("/storage")
    assert response.status_code == 200
    assert "저장 관리" in response.text
    assert "데이터별 보존기간" in response.text
