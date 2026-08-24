from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import healthmes.app as app_module
import healthmes.nutrition.intake_service as intake_service
from healthmes import clock
from healthmes.app import _initialize_activity_storage
from healthmes.nutrition import repository as nutrition_repository
from healthmes.nutrition.contracts import (
    CaptureContext,
    Confidence,
    DailyIntakeConfirmation,
    Estimate,
    EstimateKind,
    IntakeItem,
    IntakeType,
    MetadataSource,
    NutritionObservation,
    ObservationStatus,
    VisionProvenance,
)
from healthmes.nutrition.intake_contracts import IntakeIntent
from healthmes.nutrition.intake_service import (
    IntakeInteractionError,
    IntakeOperationConflict,
    create_photo_interaction,
    operation_fingerprint,
)
from healthmes.nutrition.repository import (
    persist_daily_confirmation,
    persist_observation,
)
from healthmes.storage import (
    ensure_default_policies,
    register_storage_object,
    update_retention_policy,
)
from healthmes.storage import service as storage_service
from healthmes.storage.service import DEFAULT_RETENTION
from healthmes.store import (
    Base,
    RetentionPolicy,
    StorageObject,
    WellnessEvent,
    create_db_engine,
)


def test_concurrent_sqlite_default_policy_bootstrap_is_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'retention-bootstrap.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    before_lock = threading.Barrier(2)
    real_lock = storage_service.lock_activity_write_plane
    failures: list[BaseException] = []
    observed: list[tuple[str, ...]] = []

    def synchronized_lock(session) -> None:
        before_lock.wait(timeout=5)
        real_lock(session)

    monkeypatch.setattr(
        storage_service,
        "lock_activity_write_plane",
        synchronized_lock,
    )

    def bootstrap() -> None:
        with factory() as session:
            try:
                policies = ensure_default_policies(session)
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                observed.append(
                    tuple(row.data_class for row in policies)
                )

    workers = [threading.Thread(target=bootstrap) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers)
        assert failures == []
        assert observed == [
            tuple(sorted(DEFAULT_RETENTION)),
            tuple(sorted(DEFAULT_RETENTION)),
        ]
        with factory() as session:
            rows = tuple(
                session.scalars(
                    sa.select(RetentionPolicy).order_by(
                        RetentionPolicy.data_class
                    )
                )
            )
            assert tuple(row.data_class for row in rows) == tuple(
                sorted(DEFAULT_RETENTION)
            )
    finally:
        for worker in workers:
            worker.join(timeout=5)
        engine.dispose()


def test_sqlite_nutrition_writer_waits_for_retention_shrink_and_refreshes_policy(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'retention-shrink.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    confirmed_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with factory() as session:
        ensure_default_policies(session)
        update_retention_policy(
            session,
            "nutrition_confirmation",
            "30d",
            now=confirmed_at,
        )
        session.commit()

    policy_preloaded = threading.Event()
    start_write = threading.Event()
    writer_lock_attempted = threading.Event()
    writer_finished = threading.Event()
    observed_before_write: list[int | None] = []
    failures: list[BaseException] = []
    real_lock = nutrition_repository.lock_activity_write_plane
    confirmation_id = uuid.uuid4()

    def tracked_lock(session) -> None:
        if threading.current_thread().name == "nutrition-writer":
            writer_lock_attempted.set()
        real_lock(session)

    monkeypatch.setattr(
        nutrition_repository,
        "lock_activity_write_plane",
        tracked_lock,
    )

    def write_confirmation() -> None:
        with factory() as session:
            try:
                stale_policy = session.scalar(
                    sa.select(RetentionPolicy).where(
                        RetentionPolicy.data_class
                        == "nutrition_confirmation"
                    )
                )
                assert stale_policy is not None
                observed_before_write.append(
                    stale_policy.retention_days
                )
                session.commit()
                policy_preloaded.set()
                assert start_write.wait(timeout=5)
                persist_daily_confirmation(
                    session,
                    DailyIntakeConfirmation(
                        confirmation_id=confirmation_id,
                        local_date=date(2026, 8, 10),
                        timezone="UTC",
                        observation_ids=(),
                        total_intake_complete=True,
                        confirmed_at=confirmed_at,
                        source="retention-race-test",
                    ),
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            finally:
                writer_finished.set()

    writer = threading.Thread(
        target=write_confirmation,
        name="nutrition-writer",
    )
    try:
        writer.start()
        assert policy_preloaded.wait(timeout=5)
        with factory() as shrinking:
            update_retention_policy(
                shrinking,
                "nutrition_confirmation",
                "1d",
                now=confirmed_at,
            )
            start_write.set()
            assert writer_lock_attempted.wait(timeout=5)
            assert not writer_finished.wait(timeout=0.2)
            shrinking.commit()

        writer.join(timeout=10)
        assert not writer.is_alive()
        assert failures == []
        assert observed_before_write == [30]
        with factory() as session:
            policy = session.scalar(
                sa.select(RetentionPolicy).where(
                    RetentionPolicy.data_class
                    == "nutrition_confirmation"
                )
            )
            event = session.scalar(
                sa.select(WellnessEvent).where(
                    WellnessEvent.source_provider
                    == "user-confirmation",
                    WellnessEvent.source_record_id
                    == str(confirmation_id),
                )
            )
            assert policy is not None
            assert policy.retention_days == 1
            assert event is not None
            expires_at = event.expires_at
            assert expires_at is not None
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            assert expires_at == confirmed_at + timedelta(days=1)
    finally:
        start_write.set()
        if writer.ident is not None:
            writer.join(timeout=5)
        engine.dispose()


def test_sqlite_storage_registration_waits_for_retention_shrink(
    tmp_path,
    settings,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'storage-retention-shrink.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    current = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with factory() as session:
        ensure_default_policies(session)
        update_retention_policy(
            session,
            "media",
            "30d",
            now=current,
        )
        session.commit()

    start_write = threading.Event()
    writer_lock_attempted = threading.Event()
    writer_finished = threading.Event()
    failures: list[BaseException] = []
    real_lock = storage_service.lock_activity_write_plane

    def tracked_lock(session, **kwargs) -> None:
        if threading.current_thread().name == "storage-writer":
            writer_lock_attempted.set()
        real_lock(session, **kwargs)

    monkeypatch.setattr(
        storage_service,
        "lock_activity_write_plane",
        tracked_lock,
    )

    def register_media() -> None:
        with factory() as session:
            try:
                assert start_write.wait(timeout=5)
                register_storage_object(
                    session,
                    settings,
                    relative_path="media/retention-race.bin",
                    data_class="media",
                    content_type="application/octet-stream",
                    size_bytes=1,
                    observed_at=current,
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            finally:
                writer_finished.set()

    writer = threading.Thread(
        target=register_media,
        name="storage-writer",
    )
    try:
        writer.start()
        with factory() as shrinking:
            update_retention_policy(
                shrinking,
                "media",
                "1d",
                now=current,
            )
            start_write.set()
            assert writer_lock_attempted.wait(timeout=5)
            assert not writer_finished.wait(timeout=0.2)
            shrinking.commit()

        writer.join(timeout=10)
        assert not writer.is_alive()
        assert failures == []
        with factory() as session:
            stored = session.scalar(
                sa.select(StorageObject).where(
                    StorageObject.relative_path
                    == "media/retention-race.bin"
                )
            )
            assert stored is not None
            expires_at = stored.expires_at
            assert expires_at is not None
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            assert expires_at == current + timedelta(days=1)
    finally:
        start_write.set()
        if writer.ident is not None:
            writer.join(timeout=5)
        engine.dispose()


def _seed_photo_observation(
    factory,
    settings,
    *,
    observed_at: datetime,
) -> NutritionObservation:
    media_path = f"media/{uuid.uuid4()}.jpg"
    target = settings.data_dir / media_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"photo")
    observation = NutritionObservation(
        observation_id=uuid.uuid4(),
        capture=CaptureContext(
            media_path=media_path,
            captured_at=observed_at,
            timezone="UTC",
            source="retention-race-test",
            location=None,
            metadata_provenance={
                "captured_at": MetadataSource.FIXTURE,
                "timezone": MetadataSource.FIXTURE,
                "location": MetadataSource.UNAVAILABLE,
            },
        ),
        status=ObservationStatus.USABLE,
        confidence=Confidence.HIGH,
        warnings=(),
        items=(
            IntakeItem(
                intake_type=IntakeType.BEVERAGE,
                name_candidates=("coffee",),
                category="coffee",
                serving=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="ml",
                    exact=250,
                    estimation_basis="fixture",
                ),
                caffeine=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="mg",
                    exact=95,
                    estimation_basis="fixture",
                ),
                confidence=Confidence.HIGH,
            ),
        ),
        vision=VisionProvenance(
            provider="fixture",
            model="fixture-v1",
            model_digest="sha256:fixture",
            prompt_version="photo-intake-v1",
            schema_version="nutrition-observation-v1",
            analyzed_at=observed_at + timedelta(seconds=1),
        ),
    )
    with factory() as session:
        register_storage_object(
            session,
            settings,
            relative_path=media_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=5,
            observed_at=observed_at,
        )
        persist_observation(
            session,
            settings,
            observation,
            request_fingerprint=str(observation.observation_id),
        )
        session.commit()
    return observation


def test_observation_retry_rejects_expired_stale_session_row(
    tmp_path,
    settings,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'observation-stale-retry.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    current = datetime(2026, 8, 10, 12, tzinfo=UTC)
    observed_at = current - timedelta(days=2)
    monkeypatch.setattr(clock, "utc_now", lambda: current)
    with factory() as session:
        ensure_default_policies(session)
        update_retention_policy(
            session,
            "nutrition_observation",
            "30d",
            now=current,
        )
        session.commit()
    observation = _seed_photo_observation(
        factory,
        settings,
        observed_at=observed_at,
    )
    fingerprint = str(observation.observation_id)

    try:
        with factory() as stale_session:
            stale_event = stale_session.scalar(
                sa.select(WellnessEvent).where(
                    WellnessEvent.source_provider == "sake-vlm",
                    WellnessEvent.source_record_id
                    == str(observation.observation_id),
                )
            )
            assert stale_event is not None
            stale_session.commit()

            with factory() as shrinking:
                update_retention_policy(
                    shrinking,
                    "nutrition_observation",
                    "1d",
                    now=current,
                )
                shrinking.commit()

            with pytest.raises(
                nutrition_repository.NutritionRepositoryError,
                match="expired nutrition observation",
            ):
                persist_observation(
                    stale_session,
                    settings,
                    observation,
                    request_fingerprint=fingerprint,
                )
    finally:
        engine.dispose()


def test_observation_retry_rejects_changed_request_or_media(
    tmp_path,
    settings,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'observation-id-reuse.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    observed_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
    monkeypatch.setattr(clock, "utc_now", lambda: observed_at)
    observation = _seed_photo_observation(
        factory,
        settings,
        observed_at=observed_at,
    )
    fingerprint = str(observation.observation_id)
    other_media_path = f"media/{uuid.uuid4()}.jpg"
    other_media = settings.data_dir / other_media_path
    other_media.parent.mkdir(parents=True, exist_ok=True)
    other_media.write_bytes(b"other")

    try:
        with factory() as session:
            with pytest.raises(
                nutrition_repository.NutritionRepositoryError,
                match="different request input",
            ):
                persist_observation(
                    session,
                    settings,
                    observation,
                    request_fingerprint="different-request",
                )
            register_storage_object(
                session,
                settings,
                relative_path=other_media_path,
                data_class="media",
                content_type="image/jpeg",
                size_bytes=5,
                observed_at=observed_at,
            )
            changed_media = replace(
                observation,
                capture=replace(
                    observation.capture,
                    media_path=other_media_path,
                ),
            )
            with pytest.raises(
                nutrition_repository.NutritionRepositoryError,
                match="different media",
            ):
                persist_observation(
                    session,
                    settings,
                    changed_media,
                    request_fingerprint=fingerprint,
                )
    finally:
        engine.dispose()


def test_photo_interaction_waits_for_retention_shrink_and_rejects_expired_source(
    tmp_path,
    settings,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'photo-retention-shrink.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    current = datetime(2026, 8, 10, 12, tzinfo=UTC)
    observed_at = current - timedelta(days=2)
    monkeypatch.setattr(clock, "utc_now", lambda: current)
    with factory() as session:
        ensure_default_policies(session)
        update_retention_policy(
            session,
            "nutrition_observation",
            "30d",
            now=current,
        )
        session.commit()
    observation = _seed_photo_observation(
        factory,
        settings,
        observed_at=observed_at,
    )

    start_write = threading.Event()
    writer_lock_attempted = threading.Event()
    writer_finished = threading.Event()
    failures: list[BaseException] = []
    expected_errors: list[str] = []
    real_lock = intake_service.lock_activity_write_plane
    interaction_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"photo": str(interaction_id)})

    def tracked_lock(session, **kwargs) -> None:
        if threading.current_thread().name == "photo-writer":
            writer_lock_attempted.set()
        real_lock(session, **kwargs)

    monkeypatch.setattr(
        intake_service,
        "lock_activity_write_plane",
        tracked_lock,
    )

    def create_interaction() -> None:
        with factory() as session:
            try:
                assert start_write.wait(timeout=5)
                create_photo_interaction(
                    session,
                    settings,
                    observation_id=observation.observation_id,
                    operation_id=interaction_id,
                    operation_fingerprint=fingerprint,
                    intent=IntakeIntent.LOG_CONSUMED,
                    source="retention-race-test",
                    recorded_at=current,
                )
                session.commit()
            except IntakeInteractionError as exc:
                session.rollback()
                expected_errors.append(str(exc))
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            finally:
                writer_finished.set()

    writer = threading.Thread(
        target=create_interaction,
        name="photo-writer",
    )
    try:
        writer.start()
        with factory() as shrinking:
            update_retention_policy(
                shrinking,
                "nutrition_observation",
                "1d",
                now=current,
            )
            start_write.set()
            assert writer_lock_attempted.wait(timeout=5)
            assert not writer_finished.wait(timeout=0.2)
            shrinking.commit()

        writer.join(timeout=10)
        assert not writer.is_alive()
        assert failures == []
        assert expected_errors == ["nutrition observation not found"]
        with factory() as session:
            created = tuple(
                session.scalars(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.source_record_id.in_(
                            (
                                str(interaction_id),
                                f"interaction:{interaction_id}",
                            )
                        )
                    )
                )
            )
            assert created == ()
    finally:
        start_write.set()
        if writer.ident is not None:
            writer.join(timeout=5)
        engine.dispose()


def test_photo_idempotent_retry_refreshes_stale_session_after_shrink(
    tmp_path,
    settings,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'photo-idempotent-shrink.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    current = datetime(2026, 8, 10, 12, tzinfo=UTC)
    observed_at = current - timedelta(days=2)
    monkeypatch.setattr(clock, "utc_now", lambda: current)
    with factory() as session:
        ensure_default_policies(session)
        update_retention_policy(
            session,
            "nutrition_observation",
            "30d",
            now=current,
        )
        session.commit()
    observation = _seed_photo_observation(
        factory,
        settings,
        observed_at=observed_at,
    )
    interaction_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"photo": str(interaction_id)})
    with factory() as session:
        create_photo_interaction(
            session,
            settings,
            observation_id=observation.observation_id,
            operation_id=interaction_id,
            operation_fingerprint=fingerprint,
            intent=IntakeIntent.LOG_CONSUMED,
            source="retention-race-test",
            recorded_at=current,
        )
        session.commit()
    with factory() as stale_session:
        stale_event = stale_session.scalar(
            sa.select(WellnessEvent).where(
                WellnessEvent.source_provider
                == "nutrition-interaction",
                WellnessEvent.source_record_id
                == str(interaction_id),
            )
        )
        assert stale_event is not None
        stale_session.commit()
        with factory() as shrinking:
            update_retention_policy(
                shrinking,
                "nutrition_observation",
                "1d",
                now=current,
            )
            shrinking.commit()
        with pytest.raises(
            IntakeOperationConflict,
            match="expired intake interaction",
        ):
            create_photo_interaction(
                stale_session,
                settings,
                observation_id=observation.observation_id,
                operation_id=interaction_id,
                operation_fingerprint=fingerprint,
                intent=IntakeIntent.LOG_CONSUMED,
                source="retention-race-test",
                recorded_at=current,
            )

    engine.dispose()


def test_activity_startup_explicitly_bootstraps_retention_policies(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'startup-bootstrap.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(
        app_module,
        "backfill_android_canonical_events",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "migrate_activity_summary_derivations",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "ensure_decision_domain_policies",
        lambda *_args, **_kwargs: (),
    )

    try:
        with factory() as session:
            _initialize_activity_storage(
                session,
                timezone="UTC",
                decision_owner_principal_id="owner",
            )
            session.commit()
        with factory() as session:
            assert (
                session.scalar(
                    sa.select(sa.func.count()).select_from(
                        RetentionPolicy
                    )
                )
                == len(DEFAULT_RETENTION)
            )
    finally:
        engine.dispose()
