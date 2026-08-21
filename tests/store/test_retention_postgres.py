from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from healthmes.activity.locking import (
    fenced_core_transaction,
    global_write_plane_guard,
    lock_activity_write_plane,
    session_holds_write_plane,
)
from healthmes.activity.repository import ensure_activity_policies
from healthmes.app import _initialize_activity_storage
from healthmes.storage import (
    apply_decision_retention,
    ensure_default_policies,
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.storage import service as storage_service
from healthmes.storage.service import DEFAULT_RETENTION
from healthmes.store import (
    Base,
    DecisionKind,
    DecisionRecord,
    DecisionRequestReceipt,
    ProposalStatus,
    RetentionPolicy,
    ScheduleProposal,
    StorageObject,
    Task,
    TriggerEvent,
    WellnessEvent,
    create_db_engine,
)
from healthmes.store.decision_receipts import (
    maintain_decision_receipt_results,
)
from healthmes.wearables import provenance as wearable_provenance
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
    OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
    persist_open_wearables_observation,
)


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_raw_text_dml_claims_postgres_write_plane() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_text_fence_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE raw_write_fence_probe "
                    "(value TEXT NOT NULL)"
                )
            )
        factory = sessionmaker(
            bind=engine,
            autocommit=False,
            autoflush=False,
        )
        with factory() as session:
            session.execute(sa.text("SELECT 1"))
            assert not session_holds_write_plane(session)
            session.rollback()

            session.execute(
                sa.text(
                    "INSERT INTO raw_write_fence_probe (value) "
                    "VALUES ('fenced')"
                )
            )
            assert session_holds_write_plane(session)
            session.rollback()
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_concurrent_first_startup_initializes_default_policies_once() -> None:
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
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    start = threading.Barrier(2)
    observed_counts: list[int] = []
    failures: list[BaseException] = []

    def initialize() -> None:
        with factory() as session:
            try:
                start.wait(timeout=5)
                policies = ensure_default_policies(session)
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                observed_counts.append(len(policies))

    workers = [threading.Thread(target=initialize) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers)
        assert failures == []
        assert observed_counts == [
            len(DEFAULT_RETENTION),
            len(DEFAULT_RETENTION),
        ]
        with factory() as session:
            rows = list(
                session.scalars(
                    sa.select(RetentionPolicy).order_by(
                        RetentionPolicy.data_class
                    )
                )
            )
            assert [row.data_class for row in rows] == sorted(
                DEFAULT_RETENTION
            )
    finally:
        for worker in workers:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_decision_retention_uses_exact_cutoff_and_preserves_fk_rows(
    settings,
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
    current = datetime(2026, 8, 16, 12, tzinfo=UTC)

    def decision(*, basis_at: datetime) -> DecisionRecord:
        row = DecisionRecord(
            kind=DecisionKind.INSIGHT,
            tree={"id": "healthmes-decision", "children": []},
            summary="Compact wellness outcome",
            decision_request_id=uuid.uuid4(),
            decision_turn_id=uuid.uuid4(),
            decision_request_fingerprint=uuid.uuid4().hex * 2,
            decision_payload={
                "schema": "healthmes.decision-private.v2"
            },
            decision_payload_digest=uuid.uuid4().hex * 2,
            created_at=basis_at,
        )
        return row

    try:
        with factory() as session:
            update_retention_policy(
                session,
                "decision",
                "1d",
                now=current,
            )
            at_cutoff = decision(
                basis_at=current - timedelta(days=1)
            )
            after_cutoff = decision(
                basis_at=(
                    current
                    - timedelta(days=1)
                    + timedelta(microseconds=1)
                )
            )
            legacy = DecisionRecord(
                kind=DecisionKind.SCHEDULE_CHANGE,
                tree={"id": "legacy", "children": []},
                summary="Historical non-wellness decision",
                created_at=current - timedelta(days=30),
            )
            apply_decision_retention(
                session,
                at_cutoff,
                basis_at=at_cutoff.created_at,
            )
            apply_decision_retention(
                session,
                after_cutoff,
                basis_at=after_cutoff.created_at,
            )
            session.add_all((at_cutoff, after_cutoff, legacy))
            session.flush()
            task = Task(title="Preserve proposal")
            session.add(task)
            session.flush()
            proposal = ScheduleProposal(
                task_id=task.id,
                proposed_start=current + timedelta(hours=1),
                proposed_end=current + timedelta(hours=2),
                status=ProposalStatus.PROPOSED,
                decision_record_id=at_cutoff.id,
            )
            session.add(proposal)
            session.commit()
            ids = (
                at_cutoff.id,
                after_cutoff.id,
                legacy.id,
                proposal.id,
            )

        with factory() as session:
            report = run_storage_maintenance(
                session,
                settings,
                now=current,
            )
            session.commit()

        with factory() as session:
            at_cutoff_id, after_cutoff_id, legacy_id, proposal_id = ids
            assert report.decision_candidates == 1
            assert report.decisions_deleted == 1
            assert session.get(DecisionRecord, at_cutoff_id) is None
            assert (
                session.get(DecisionRecord, after_cutoff_id)
                is not None
            )
            assert session.get(DecisionRecord, legacy_id) is not None
            retained_proposal = session.get(
                ScheduleProposal,
                proposal_id,
            )
            assert retained_proposal is not None
            assert retained_proposal.decision_record_id is None
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_retention_extension_cannot_revive_expired_receipt() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_receipt_monotonic_{uuid.uuid4().hex}"
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
    requested_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    original_deadline = requested_at + timedelta(days=1)
    current = original_deadline + timedelta(hours=1)
    identity_expires_at = requested_at + timedelta(days=30)
    receipt_id: uuid.UUID | None = None
    try:
        with factory() as session:
            update_retention_policy(
                session,
                "decision",
                "1d",
                now=requested_at,
            )
            receipt = DecisionRequestReceipt(
                request_id=uuid.uuid4(),
                request_fingerprint="f" * 64,
                requested_at=requested_at,
                state="completed",
                result_payload={
                    "schema": "healthmes.decision-receipt.v1",
                    "result": {"answer": "expired sensitive answer"},
                },
                result_expires_at=original_deadline,
                retention_basis_at=requested_at,
                expires_at=identity_expires_at,
            )
            session.add(receipt)
            session.commit()
            receipt_id = receipt.id

        with factory() as session:
            update_retention_policy(
                session,
                "decision",
                "30d",
                now=current,
            )
            session.commit()

        with factory() as session:
            stored = session.get(DecisionRequestReceipt, receipt_id)
            assert stored is not None
            assert stored.state == "tombstone"
            assert stored.result_payload is None
            assert stored.result_expires_at is None
            assert stored.expires_at == identity_expires_at
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_receipt_maintenance_advances_in_bounded_json_batches(
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_receipt_batch_{uuid.uuid4().hex}"
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
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    record_id = uuid.uuid4()
    try:
        with factory() as session:
            legacy = DecisionRequestReceipt(
                request_id=uuid.uuid4(),
                request_fingerprint="a" * 64,
                requested_at=now,
                state="completed",
                result_payload={
                    "schema": "healthmes.decision-receipt.v1",
                    "result": {
                        "answer": "legacy sensitive answer",
                        "persistence_status": "persisted",
                        "decision_record_id": str(record_id),
                    },
                },
                result_expires_at=now + timedelta(days=30),
                retention_basis_at=now,
                expires_at=now + timedelta(days=30),
            )
            expired = DecisionRequestReceipt(
                request_id=uuid.uuid4(),
                request_fingerprint="b" * 64,
                requested_at=now,
                state="completed",
                result_payload={
                    "schema": "healthmes.decision-receipt.v2",
                    "kind": "transient_result",
                    "result": {"answer": "expired transient answer"},
                },
                result_expires_at=now + timedelta(days=30),
                retention_basis_at=now - timedelta(minutes=15),
                expires_at=now + timedelta(days=30),
            )
            fresh_pointer = DecisionRequestReceipt(
                request_id=uuid.uuid4(),
                request_fingerprint="c" * 64,
                requested_at=now,
                state="completed",
                result_payload={
                    "schema": "healthmes.decision-receipt.v2",
                    "kind": "decision_record",
                    "decision_record_id": str(uuid.uuid4()),
                },
                result_expires_at=now + timedelta(days=30),
                retention_basis_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=30),
            )
            session.add_all((legacy, expired, fresh_pointer))
            session.commit()
            legacy_id = legacy.id
            expired_id = expired.id
            pointer_id = fresh_pointer.id

        processed = []
        cursor = None
        for _index in range(4):
            with factory() as session:
                batch = maintain_decision_receipt_results(
                    session,
                    now=now,
                    batch_size=1,
                    after_id=cursor,
                )
                processed.append(batch.scanned)
                cursor = batch.next_cursor
                session.commit()
        assert processed == [1, 1, 1, 0]

        with factory() as session:
            compacted = session.get(
                DecisionRequestReceipt,
                legacy_id,
            )
            scrubbed = session.get(
                DecisionRequestReceipt,
                expired_id,
            )
            retained = session.get(
                DecisionRequestReceipt,
                pointer_id,
            )
            assert compacted is not None
            assert compacted.result_payload == {
                "schema": "healthmes.decision-receipt.v2",
                "kind": "decision_record",
                "decision_record_id": str(record_id),
            }
            assert scrubbed is not None
            assert scrubbed.state == "tombstone"
            assert scrubbed.result_payload is None
            assert retained is not None
            assert retained.state == "completed"
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_retention_locks_trigger_before_linked_decision() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_retention_lock_order_{uuid.uuid4().hex}"
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
    current = datetime(2026, 8, 17, 12, tzinfo=UTC)
    retention_started = threading.Event()
    retention_pid: list[int] = []
    failures: list[BaseException] = []
    worker: threading.Thread | None = None
    try:
        with factory() as session:
            update_retention_policy(
                session,
                "decision",
                "30d",
                now=current,
            )
            event = TriggerEvent(
                fired_at=current,
                rule_id="retention_lock_order",
                alert_sent=False,
                payload={
                    "push": {
                        "state": "dispatching",
                        "channel": "decision",
                    }
                },
            )
            session.add(event)
            session.flush()
            record = DecisionRecord(
                kind=DecisionKind.INSIGHT,
                tree={"id": "lock-order", "children": []},
                summary="Linked decision lock-order probe",
                trigger_event_id=event.id,
                decision_request_id=uuid.uuid4(),
                decision_turn_id=uuid.uuid4(),
                decision_request_fingerprint=uuid.uuid4().hex * 2,
                decision_payload={"schema": "healthmes.decision-private.v2"},
                decision_payload_digest=uuid.uuid4().hex * 2,
                created_at=current,
            )
            apply_decision_retention(
                session,
                record,
                basis_at=current,
            )
            session.add(record)
            session.commit()
            event_id = event.id
            record_id = record.id

        def shorten_retention() -> None:
            with factory() as session:
                try:
                    retention_pid.append(
                        int(
                            session.scalar(
                                sa.text("SELECT pg_backend_pid()")
                            )
                        )
                    )
                    retention_started.set()
                    update_retention_policy(
                        session,
                        "decision",
                        "14d",
                        now=current,
                    )
                    session.commit()
                except BaseException as exc:
                    session.rollback()
                    failures.append(exc)

        with factory() as dispatch:
            locked_event = dispatch.scalar(
                sa.select(TriggerEvent)
                .where(TriggerEvent.id == event_id)
                .with_for_update()
            )
            assert locked_event is not None
            worker = threading.Thread(
                target=shorten_retention,
                name="decision-retention-lock-order",
            )
            worker.start()
            assert retention_started.wait(timeout=5)

            deadline = time.monotonic() + 5
            waiting_for_trigger = False
            while time.monotonic() < deadline and worker.is_alive():
                wait_event_type = dispatch.scalar(
                    sa.text(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE pid = :pid"
                    ),
                    {"pid": retention_pid[0]},
                )
                if wait_event_type == "Lock":
                    waiting_for_trigger = True
                    break
                time.sleep(0.05)
            assert waiting_for_trigger

            dispatch.execute(
                sa.text("SET LOCAL lock_timeout = '1s'")
            )
            locked_record = dispatch.scalar(
                sa.select(DecisionRecord)
                .where(DecisionRecord.id == record_id)
                .with_for_update()
            )
            assert locked_record is not None
            dispatch.commit()

        worker.join(timeout=10)
        assert not worker.is_alive()
        assert failures == []
    finally:
        if worker is not None:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.parametrize("bootstrap_path", ("startup", "maintenance"))
@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_activity_bootstrap_takes_write_plane_before_policy_inserts(
    settings,
    bootstrap_path,
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
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    worker_started = threading.Event()
    failures: list[BaseException] = []
    maintenance_guard_attempted = threading.Event()
    if bootstrap_path == "maintenance":
        real_guard = storage_service.global_write_plane_guard

        @contextmanager
        def tracked_guard(bind):
            maintenance_guard_attempted.set()
            with real_guard(bind) as connection:
                yield connection

        monkeypatch.setattr(
            storage_service,
            "global_write_plane_guard",
            tracked_guard,
        )

    def bootstrap() -> None:
        with factory() as session:
            worker_started.set()
            try:
                if bootstrap_path == "startup":
                    _initialize_activity_storage(
                        session,
                        timezone="UTC",
                        decision_owner_principal_id=(
                            settings.decision_owner_principal_id
                        ),
                    )
                else:
                    run_storage_maintenance(
                        session,
                        settings,
                        now=datetime(2026, 8, 10, 12, tzinfo=UTC),
                    )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    worker: threading.Thread | None = None
    try:
        with factory() as first:
            lock_activity_write_plane(first)
            worker = threading.Thread(target=bootstrap)
            worker.start()
            assert worker_started.wait(timeout=5)

            if bootstrap_path == "maintenance":
                # Same-process writers stop at the process lock before they
                # attempt the cross-process database guard.
                assert not maintenance_guard_attempted.wait(timeout=0.2)
            else:
                time.sleep(0.2)

            assert worker.is_alive()
            assert (
                first.scalar(
                    sa.select(sa.func.count()).select_from(RetentionPolicy)
                )
                == 0
            )
            policies = ensure_activity_policies(first)
            assert set(policies) == {
                "activity_raw",
                "activity_hourly",
                "activity_daily",
            }
            first.commit()
            worker.join(timeout=10)

        assert worker is not None
        assert not worker.is_alive()
        assert failures == []
        if bootstrap_path == "maintenance":
            assert maintenance_guard_attempted.is_set()
        with factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(RetentionPolicy)
            ) == len(DEFAULT_RETENTION)
    finally:
        if worker is not None:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_wearable_writer_and_retention_shrink_share_write_fence(
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
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with factory() as session:
        update_retention_policy(
            session,
            OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
            "30d",
            now=now,
        )
        session.commit()

    policy_read = threading.Event()
    release_writer = threading.Event()
    original_policy = wearable_provenance._retention_policy

    def paused_policy(session):
        policy = original_policy(session)
        if not policy_read.is_set():
            policy_read.set()
            assert release_writer.wait(timeout=10)
        return policy

    monkeypatch.setattr(
        wearable_provenance,
        "_retention_policy",
        paused_policy,
    )
    failures: list[BaseException] = []
    updater_started = threading.Event()

    def write_snapshot() -> None:
        with factory() as session:
            try:
                persist_open_wearables_observation(
                    session,
                    normalized_context={
                        "date": "2026-08-10",
                        "status": "ok",
                    },
                    local_day=date(2026, 8, 10),
                    timezone="UTC",
                    collected_at=now,
                    now=now,
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    def shrink_retention() -> None:
        with factory() as session:
            updater_started.set()
            try:
                storage_service._update_retention_policy(
                    session,
                    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
                    "1d",
                    now=now,
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    writer = threading.Thread(target=write_snapshot)
    updater: threading.Thread | None = None
    try:
        writer.start()
        assert policy_read.wait(timeout=5)
        updater = threading.Thread(target=shrink_retention)
        updater.start()
        assert updater_started.wait(timeout=5)

        # The writer already owns the process-first lock order. The updater
        # must not reach or mutate PostgreSQL until that writer releases it.
        time.sleep(0.2)
        assert updater.is_alive()
        with factory() as observer:
            policy = observer.scalar(
                sa.select(RetentionPolicy).where(
                    RetentionPolicy.data_class
                    == OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS
                )
            )
            assert policy is not None
            assert policy.retention_days == 30
        release_writer.set()
        writer.join(timeout=10)
        updater.join(timeout=10)

        assert not writer.is_alive()
        assert not updater.is_alive()
        assert failures == []
        with factory() as session:
            policy = session.scalar(
                sa.select(RetentionPolicy).where(
                    RetentionPolicy.data_class
                    == OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS
                )
            )
            assert policy is not None
            assert policy.retention_days == 1
            rows = session.scalars(
                sa.select(WellnessEvent).where(
                    WellnessEvent.source_provider
                    == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
                )
            ).all()
            assert len(rows) == 2
            assert {
                row.expires_at
                for row in rows
            } == {
                datetime(2026, 8, 11, tzinfo=UTC)
            }
    finally:
        release_writer.set()
        writer.join(timeout=5)
        if updater is not None:
            updater.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_direct_core_dml_requires_fenced_transaction() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_core_fence_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE core_write_fence_probe "
                    "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
            )

        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.execute(
                    sa.text(
                        "INSERT INTO core_write_fence_probe (id, value) "
                        "VALUES (1, 'unfenced')"
                    )
                )

        with fenced_core_transaction(engine) as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO core_write_fence_probe (id, value) "
                    "VALUES (1, 'fenced')"
                )
            )

        with engine.connect() as connection:
            assert connection.execute(
                sa.text(
                    "SELECT id, value FROM core_write_fence_probe "
                    "ORDER BY id"
                )
            ).all() == [(1, "fenced")]
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_fenced_core_reuses_caller_connection_with_pool_size_one() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_caller_guard_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE caller_guard_probe "
                    "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
            )

        with engine.connect() as supplied_connection:
            original_isolation = supplied_connection.get_isolation_level()
            with fenced_core_transaction(
                supplied_connection,
                timeout_seconds=1,
            ) as guard_connection:
                assert guard_connection is supplied_connection
                guard_connection.execute(
                    sa.text(
                        "INSERT INTO caller_guard_probe (id, value) "
                        "VALUES (1, 'same connection')"
                    )
                )

            assert not supplied_connection.closed
            assert (
                supplied_connection.get_isolation_level()
                == original_isolation
            )
            assert supplied_connection.execute(
                sa.text(
                    "SELECT id, value FROM caller_guard_probe "
                    "ORDER BY id"
                )
            ).all() == [(1, "same connection")]
            supplied_connection.rollback()
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_guard_rejects_caller_connection_with_active_transaction() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    engine = create_db_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
    )
    try:
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
            with pytest.raises(
                RuntimeError,
                match="without an active transaction",
            ):
                with global_write_plane_guard(connection):
                    pytest.fail("active caller connection unexpectedly guarded")
            connection.rollback()
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_same_url_session_is_not_mistaken_for_guarded_connection() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    engine = create_db_engine(
        database_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
    )
    try:
        with global_write_plane_guard(engine) as guard_connection:
            assert guard_connection is not None
            with Session(engine) as unrelated:
                assert not session_holds_write_plane(unrelated)
                with pytest.raises(
                    TimeoutError,
                    match="activity write plane",
                ):
                    lock_activity_write_plane(
                        unrelated,
                        timeout_seconds=0.1,
                        poll_seconds=0.01,
                    )
                unrelated.rollback()
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_maintenance_reuses_guard_connection_for_both_commits(
    settings,
    monkeypatch,
    tmp_path,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_maintenance_guard_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    Base.metadata.create_all(engine)
    current = datetime(2026, 8, 18, 12, tzinfo=UTC)
    payload = b"guarded PostgreSQL maintenance"
    data_dir = tmp_path / "postgres-maintenance"
    target = data_dir / "raw_ingest" / "guarded.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    live_settings = settings.model_copy(
        update={
            "database_url": str(engine.url),
            "data_dir": data_dir,
        }
    )
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    with factory() as setup:
        obj = StorageObject(
            data_class="raw_payload",
            relative_path="raw_ingest/guarded.bin",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=sha256(payload).hexdigest(),
            retention_basis_at=current - timedelta(days=30),
            expires_at=current - timedelta(days=1),
            safe_to_purge=True,
        )
        setup.add(obj)
        setup.commit()
        object_id = obj.id

    real_guard = storage_service.global_write_plane_guard
    guard_pids: list[int] = []

    @contextmanager
    def tracked_guard(bind):
        with real_guard(bind) as connection:
            assert connection is not None
            guard_pids.append(
                int(connection.scalar(sa.text("SELECT pg_backend_pid()")))
            )
            # The session-scoped advisory lock survives this commit. End the
            # observation transaction so the maintenance Session owns and
            # commits its own two root transactions on the guarded connection.
            connection.commit()
            yield connection

    real_commit = Session.commit
    maintenance_commit_pids: list[int] = []

    def tracked_commit(current_session):
        bind = current_session.get_bind()
        if isinstance(bind, Connection):
            maintenance_commit_pids.append(
                int(
                    current_session.scalar(
                        sa.text("SELECT pg_backend_pid()")
                    )
                )
            )
        return real_commit(current_session)

    real_expire_all = Session.expire_all
    caller_expirations: list[int] = []
    caller = factory()

    def tracked_expire_all(current_session):
        if current_session is caller:
            caller_expirations.append(id(current_session))
        return real_expire_all(current_session)

    monkeypatch.setattr(
        storage_service,
        "global_write_plane_guard",
        tracked_guard,
    )
    monkeypatch.setattr(Session, "commit", tracked_commit)
    monkeypatch.setattr(
        Session,
        "expire_all",
        tracked_expire_all,
    )

    try:
        assert caller.get_bind() is engine
        report = run_storage_maintenance(
            caller,
            live_settings,
            now=current,
        )

        assert report.deleted == 1
        assert report.bytes_reclaimed == len(payload)
        assert guard_pids and len(guard_pids) == 1
        assert maintenance_commit_pids == [
            guard_pids[0],
            guard_pids[0],
        ]
        assert caller_expirations == [id(caller)]
        assert caller.get_bind() is engine
        assert not target.exists()

        stored = caller.get(StorageObject, object_id)
        assert stored is not None
        assert stored.purged_at == current
        assert stored.file_cleanup_completed_at is not None
    finally:
        caller.close()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()
