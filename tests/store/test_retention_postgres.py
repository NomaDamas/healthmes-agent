from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from healthmes.activity.locking import lock_activity_write_plane
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
    worker_pid: list[int] = []
    failures: list[BaseException] = []

    def bootstrap() -> None:
        with factory() as session:
            worker_pid.append(
                int(session.scalar(sa.text("SELECT pg_backend_pid()")))
            )
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

            deadline = time.monotonic() + 5
            waiting_for_advisory_lock = False
            while time.monotonic() < deadline and worker.is_alive():
                wait_event = first.execute(
                    sa.text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": worker_pid[0]},
                ).one_or_none()
                if wait_event is not None and tuple(wait_event) == (
                    "Lock",
                    "advisory",
                ):
                    waiting_for_advisory_lock = True
                    break
                time.sleep(0.05)

            assert waiting_for_advisory_lock
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
    updater_pid: list[int] = []
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
            updater_pid.append(
                int(
                    session.scalar(
                        sa.text("SELECT pg_backend_pid()")
                    )
                )
            )
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

        deadline = time.monotonic() + 5
        waiting_for_advisory_lock = False
        while (
            time.monotonic() < deadline
            and updater.is_alive()
        ):
            with factory() as observer:
                wait_event = observer.execute(
                    sa.text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": updater_pid[0]},
                ).one_or_none()
            if wait_event is not None and tuple(wait_event) == (
                "Lock",
                "advisory",
            ):
                waiting_for_advisory_lock = True
                break
            time.sleep(0.05)

        assert waiting_for_advisory_lock
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
