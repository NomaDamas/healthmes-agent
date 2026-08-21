import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from healthmes.durable_files import (
    MaintenanceBudget,
    MaintenanceBudgetExceeded,
)
from healthmes.storage import register_storage_object, run_storage_maintenance
from healthmes.storage import service as storage_service
from healthmes.store import (
    Base,
    RawIngestEvent,
    StorageObject,
    StorageUsageDaily,
    create_db_engine,
)


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'storage-lifecycle.db'}"
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _payload_paths(settings, payload: bytes):
    digest = hashlib.sha256(payload).hexdigest()
    filename = f"000000_000000-{digest[:12]}.bin"
    relative_path = f"raw_ingest/2026/07/01/{filename}"
    destination = settings.data_dir / relative_path
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    return digest, relative_path, destination, staged


def test_scheduled_storage_lifecycle_shares_one_maintenance_budget(
    settings,
    monkeypatch,
):
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=1024,
        max_directory_entries=64,
    )
    engine = object()
    session = object()
    observed: list[tuple[str, object]] = []

    monkeypatch.setattr(
        storage_service,
        "_storage_maintenance_budget",
        lambda _settings: budget,
    )
    monkeypatch.setattr(storage_service, "get_engine", lambda: engine)

    def reconcile(bind, current_settings, **kwargs):
        assert bind is engine
        assert current_settings is settings
        observed.append(("staging", kwargs["maintenance_budget"]))
        return SimpleNamespace(
            unresolved=0,
            truncated=False,
            errors=(),
        )

    def maintain(current_session, current_settings, **kwargs):
        assert current_session is session
        assert current_settings is settings
        observed.append(("retention", kwargs["maintenance_budget"]))

    @contextmanager
    def fake_session_scope():
        yield session

    monkeypatch.setattr(
        storage_service,
        "reconcile_staging_files",
        reconcile,
    )
    monkeypatch.setattr(
        storage_service,
        "run_storage_maintenance",
        maintain,
    )
    monkeypatch.setattr(
        storage_service,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        storage_service,
        "measure_usage",
        lambda *_args, **_kwargs: {},
    )

    storage_service.build_storage_maintenance_job(settings)()

    assert observed == [
        ("staging", budget),
        ("retention", budget),
    ]


def test_retention_removes_final_and_staging_hard_link_as_one_payload(
    engine,
    settings,
):
    payload = b"published payload with crash-left staging link"
    digest, relative_path, destination, staged = _payload_paths(
        settings,
        payload,
    )
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        session.commit()

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 1
        assert report.files_deleted == 1
        assert report.deleted == 1
        assert report.file_cleanup_pending == 0
        assert report.bytes_reclaimed == len(payload)
        assert obj.file_cleanup_identity["version"] == 2
        assert obj.file_cleanup_completed_at is not None
        assert not destination.exists()
        assert not staged.exists()
        assert (
            list(settings.data_dir.rglob(".healthmes-unlink-*"))
            == []
        )


def test_usage_counts_final_and_staging_hard_link_once(
    engine,
    settings,
):
    payload = b"published payload with crash-left staging link"
    digest, relative_path, destination, staged = _payload_paths(
        settings,
        payload,
    )
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)

    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        session.commit()

        usage = storage_service.measure_usage(session, settings)

    assert usage == {
        "raw_payload": {
            "bytes": len(payload),
            "objects": 1,
        }
    }


def test_legacy_cleanup_retry_upgrades_and_removes_remaining_staging_link(
    engine,
    settings,
):
    payload = b"legacy cleanup with one remaining staging hard link"
    digest, relative_path, destination, staged = _payload_paths(
        settings,
        payload,
    )
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)
    metadata = destination.stat()

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = {
            "version": 1,
            "kind": "regular",
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns,
            "nlink": metadata.st_nlink,
            "sha256": digest,
        }
        session.commit()
        destination.unlink()

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 1
        assert report.deleted == 1
        assert report.file_cleanup_pending == 0
        assert report.bytes_reclaimed == len(payload)
        assert obj.file_cleanup_identity["version"] == 2
        assert obj.file_cleanup_completed_at is not None
        assert not staged.exists()


def test_pre_identity_purge_acknowledges_missing_file(
    engine,
    settings,
):
    payload = b"legacy payload already removed before cleanup identities"
    digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    _staged.parent.mkdir(parents=True)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = None
        session.commit()

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 0
        assert report.file_cleanup_pending == 0
        assert report.bytes_reclaimed == 0
        assert report.errors == ()
        assert obj.file_cleanup_identity == {
            "version": 2,
            "kind": "missing",
            "aliases": [],
        }
        assert obj.file_cleanup_completed_at is not None
        assert not destination.exists()


def test_pre_identity_purge_acknowledges_missing_file_with_invalid_digest(
    engine,
    settings,
):
    payload = b"legacy payload removed despite corrupt indexed digest"
    _digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    _staged.parent.mkdir(parents=True)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=None,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.sha256 = "not-a-valid-sha256"
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = None
        session.commit()

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 0
        assert report.file_cleanup_pending == 0
        assert report.bytes_reclaimed == 0
        assert report.errors == ()
        assert obj.file_cleanup_identity == {
            "version": 2,
            "kind": "missing",
            "aliases": [],
        }
        assert obj.file_cleanup_completed_at is not None
        assert not destination.exists()


@pytest.mark.parametrize("unavailable", ["data_root", "intermediate_parent"])
def test_pre_identity_purge_does_not_treat_unavailable_ancestor_as_missing(
    engine,
    settings,
    tmp_path,
    unavailable,
):
    payload = b"legacy plaintext on a temporarily unavailable storage volume"
    _digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=None,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.sha256 = "not-a-valid-sha256"
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = None
        session.commit()

        if unavailable == "data_root":
            detached = tmp_path / "detached-data-root"
            settings.data_dir.rename(detached)
            restore_from = detached
            restore_to = settings.data_dir
        else:
            ancestor = settings.data_dir / "raw_ingest" / "2026"
            detached = settings.data_dir / "raw_ingest" / "detached-2026"
            ancestor.rename(detached)
            restore_from = detached
            restore_to = ancestor
        try:
            report = run_storage_maintenance(
                session,
                settings,
                now=datetime(2026, 8, 6, tzinfo=UTC),
            )
        finally:
            restore_from.rename(restore_to)

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 0
        assert report.file_cleanup_pending == 1
        assert report.bytes_reclaimed == 0
        assert report.errors
        assert obj.file_cleanup_identity is None
        assert obj.file_cleanup_completed_at is None
        assert destination.read_bytes() == payload


@pytest.mark.skipif(os.name == "nt", reason="POSIX legacy quarantine contract")
@pytest.mark.parametrize("indexed_sha256", [None, "not-a-valid-sha256"])
def test_pre_identity_purge_preserves_unverifiable_legacy_unlink_quarantine(
    engine,
    settings,
    indexed_sha256,
):
    payload = b"legacy quarantine plaintext requires operator review"
    _digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    _staged.parent.mkdir(parents=True)
    quarantine = destination.with_name(
        f".healthmes-unlink-{uuid.uuid4().hex}-{destination.name}"
    )

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=None,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.sha256 = indexed_sha256
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = None
        session.commit()
        os.rename(destination, quarantine)

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 0
        assert report.file_cleanup_pending == 1
        assert report.bytes_reclaimed == 0
        assert any(
            "legacy durable-unlink quarantine" in error
            and "manual review" in error
            for error in report.errors
        )
        assert obj.file_cleanup_identity is None
        assert obj.file_cleanup_completed_at is None
        assert not destination.exists()
        assert quarantine.read_bytes() == payload


def test_pre_identity_purge_preserves_unverifiable_remaining_file(
    engine,
    settings,
):
    payload = b"legacy payload without indexed digest"
    _digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=None,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = None
        session.commit()

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 0
        assert report.file_cleanup_pending == 1
        assert report.bytes_reclaimed == 0
        assert len(report.errors) == 1
        assert "no indexed SHA-256" in report.errors[0]
        assert obj.file_cleanup_identity is None
        assert obj.file_cleanup_completed_at is None
        assert destination.read_bytes() == payload


def test_pre_identity_purge_preserves_file_with_invalid_digest(
    engine,
    settings,
):
    payload = b"legacy payload with corrupt indexed digest"
    _digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=None,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.sha256 = "not-a-valid-sha256"
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = None
        session.commit()

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 0
        assert report.file_cleanup_pending == 1
        assert report.bytes_reclaimed == 0
        assert len(report.errors) == 1
        assert "invalid indexed SHA-256" in report.errors[0]
        assert obj.file_cleanup_identity is None
        assert obj.file_cleanup_completed_at is None
        assert destination.read_bytes() == payload


@pytest.mark.skipif(os.name == "nt", reason="POSIX legacy quarantine contract")
def test_legacy_cleanup_retry_removes_crash_left_durable_unlink_quarantine(
    engine,
    settings,
):
    payload = b"legacy durable unlink quarantine must remain discoverable"
    digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    metadata = destination.stat()
    quarantine = destination.with_name(
        f".healthmes-unlink-{uuid.uuid4().hex}-{destination.name}"
    )

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
        obj.file_cleanup_identity = {
            "version": 1,
            "kind": "regular",
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns,
            "nlink": metadata.st_nlink,
            "sha256": digest,
        }
        session.commit()
        os.rename(destination, quarantine)

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 1
        assert report.file_cleanup_pending == 0
        assert report.bytes_reclaimed == len(payload)
        assert obj.file_cleanup_identity["version"] == 2
        assert obj.file_cleanup_completed_at is not None
        assert not destination.exists()
        assert not quarantine.exists()


def test_retention_fails_closed_for_unknown_hard_link(
    engine,
    settings,
):
    payload = b"payload with an unexplained external hard link"
    digest, relative_path, destination, staged = _payload_paths(
        settings,
        payload,
    )
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)
    unknown = settings.data_dir / "unexpected-copy.bin"
    os.link(destination, unknown)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        session.commit()

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 0
        assert report.files_deleted == 0
        assert report.deleted == 0
        assert report.file_cleanup_pending == 0
        assert len(report.errors) == 1
        assert "unknown hard links" in report.errors[0]
        assert obj.purged_at is None
        assert destination.read_bytes() == payload
        assert staged.read_bytes() == payload
        assert unknown.read_bytes() == payload


def test_cleanup_rejects_hard_link_substituted_after_identity_capture(
    engine,
    settings,
    monkeypatch,
):
    payload = b"known staging link replaced by an unknown hard link"
    digest, relative_path, destination, staged = _payload_paths(
        settings,
        payload,
    )
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)
    unknown = settings.data_dir / "unknown-after-capture.bin"

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        session.commit()
        real_cleanup = storage_service._cleanup_purged_files

        def substitute_link(configured, candidates, **kwargs):
            staged.unlink()
            os.link(destination, unknown)
            return real_cleanup(configured, candidates, **kwargs)

        monkeypatch.setattr(
            storage_service,
            "_cleanup_purged_files",
            substitute_link,
        )

        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )

        session.refresh(obj)
        assert report.records_purged == 1
        assert report.files_deleted == 0
        assert report.deleted == 0
        assert report.file_cleanup_pending == 1
        assert report.bytes_reclaimed == 0
        assert len(report.errors) == 1
        assert "unknown hard links replacing" in report.errors[0]
        assert obj.file_cleanup_completed_at is None
        assert destination.read_bytes() == payload
        assert not staged.exists()
        assert unknown.read_bytes() == payload

        monkeypatch.setattr(
            storage_service,
            "_cleanup_purged_files",
            real_cleanup,
        )
        retry = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        session.refresh(obj)
        assert retry.records_purged == 0
        assert retry.files_deleted == 0
        assert retry.file_cleanup_pending == 1
        assert retry.bytes_reclaimed == 0
        assert len(retry.errors) == 1
        assert "unknown hard links replacing" in retry.errors[0]
        assert obj.file_cleanup_completed_at is None
        assert destination.read_bytes() == payload
        assert unknown.read_bytes() == payload

        unknown.unlink()
        recovered = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 7, tzinfo=UTC),
        )
        session.refresh(obj)
        assert recovered.files_deleted == 1
        assert recovered.file_cleanup_pending == 0
        assert recovered.bytes_reclaimed == len(payload)
        assert recovered.errors == ()
        assert obj.file_cleanup_completed_at is not None
        assert not destination.exists()


def test_cleanup_hard_link_created_during_final_unlink_never_auto_completes(
    engine,
    settings,
    monkeypatch,
):
    payload = b"generation stranded behind an unknown final hard link"
    digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    unknown = settings.data_dir / "unknown-during-final-unlink.bin"

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        session.commit()
        real_unlink = storage_service._unlink_cleanup_entry
        raced = False

        def add_unknown_link_before_unlink(parent):
            nonlocal raced
            if (
                parent.name == storage_service._CLEANUP_QUARANTINE_ENTRY
                and not raced
            ):
                raced = True
                os.link(parent.path / parent.name, unknown)
            return real_unlink(parent)

        monkeypatch.setattr(
            storage_service,
            "_unlink_cleanup_entry",
            add_unknown_link_before_unlink,
        )
        first = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )
        session.refresh(obj)

        assert raced is True
        assert first.records_purged == 1
        assert first.files_deleted == 0
        assert first.file_cleanup_pending == 1
        assert first.bytes_reclaimed == 0
        assert len(first.errors) == 1
        assert "still has unknown hard links" in first.errors[0]
        assert obj.file_cleanup_identity["manual_review_required"] == (
            "unknown_hard_links_after_cleanup"
        )
        assert obj.file_cleanup_completed_at is None
        assert not destination.exists()
        assert unknown.read_bytes() == payload

        monkeypatch.setattr(
            storage_service,
            "_unlink_cleanup_entry",
            real_unlink,
        )
        retry = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        retry_job = session.get(
            storage_service.PurgeJob,
            uuid.UUID(retry.job_id),
        )
        session.refresh(obj)

        assert retry.records_purged == 0
        assert retry.files_deleted == 0
        assert retry.file_cleanup_pending == 1
        assert retry.bytes_reclaimed == 0
        assert len(retry.errors) == 1
        assert "requires manual review" in retry.errors[0]
        assert retry_job is not None
        assert retry_job.status == "pending_file_cleanup"
        assert obj.file_cleanup_completed_at is None
        assert unknown.read_bytes() == payload


def test_cleanup_hard_link_race_survives_manual_review_commit_failure(
    engine,
    settings,
    monkeypatch,
):
    payload = b"manual review must survive a failed database commit"
    digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    unknown = settings.data_dir / "unknown-after-failed-review-commit.bin"

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        object_id = obj.id
        session.commit()
        real_unlink = storage_service._unlink_cleanup_entry
        real_commit = Session.commit
        commit_calls = 0
        raced = False

        def add_unknown_link_before_unlink(parent):
            nonlocal raced
            if (
                parent.name == storage_service._CLEANUP_QUARANTINE_ENTRY
                and not raced
            ):
                raced = True
                os.link(parent.path / parent.name, unknown)
            return real_unlink(parent)

        def fail_manual_review_commit(current_session):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                raise OSError("injected manual-review commit failure")
            real_commit(current_session)

        monkeypatch.setattr(
            storage_service,
            "_unlink_cleanup_entry",
            add_unknown_link_before_unlink,
        )
        monkeypatch.setattr(Session, "commit", fail_manual_review_commit)
        with pytest.raises(
            OSError,
            match="manual-review commit failure",
        ):
            run_storage_maintenance(
                session,
                settings,
                now=datetime(2026, 8, 5, tzinfo=UTC),
            )

        assert raced is True
        assert not destination.exists()
        assert unknown.read_bytes() == payload
        journal_dir = (
            settings.data_dir
            / storage_service._CLEANUP_JOURNAL_DIRECTORY
        )
        assert len(list(journal_dir.glob(f"*{object_id.hex}-intent.json"))) == 1
        assert (
            len(
                list(
                    journal_dir.glob(
                        f"*{object_id.hex}-manual-review.json"
                    )
                )
            )
            == 1
        )
        with Session(engine) as verification:
            stored = verification.get(StorageObject, object_id)
            assert stored is not None
            assert stored.purged_at is not None
            assert stored.file_cleanup_completed_at is None
            assert (
                stored.file_cleanup_identity.get("manual_review_required")
                is None
            )

        monkeypatch.setattr(Session, "commit", real_commit)
        monkeypatch.setattr(
            storage_service,
            "_unlink_cleanup_entry",
            real_unlink,
        )
        retry = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        session.refresh(obj)

        assert retry.records_purged == 0
        assert retry.files_deleted == 0
        assert retry.file_cleanup_pending == 1
        assert retry.bytes_reclaimed == 0
        assert len(retry.errors) == 1
        assert "durable journal records an ambiguous" in retry.errors[0]
        assert obj.file_cleanup_identity["manual_review_required"] == (
            "unknown_hard_links_after_cleanup"
        )
        assert obj.file_cleanup_completed_at is None
        assert unknown.read_bytes() == payload


def test_unlink_quarantine_is_never_indexed_or_measured_as_raw_payload(
    engine,
    settings,
):
    quarantine = (
        settings.data_dir
        / "raw_ingest"
        / f".healthmes-unlink-{uuid.uuid4().hex}-payload.bin"
    )
    quarantine.parent.mkdir(parents=True)
    quarantine.write_bytes(b"operator-review quarantine")
    relative_path = quarantine.relative_to(settings.data_dir).as_posix()

    with Session(engine) as session:
        storage_service._discover_unindexed(session, settings)
        session.commit()
        usage = storage_service.measure_usage(session, settings)
        session.flush()

        assert session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path == relative_path
            )
        ) is None
        assert usage.get(
            "raw_payload",
            {"bytes": 0, "objects": 0},
        ) == {"bytes": 0, "objects": 0}
        assert quarantine.read_bytes() == b"operator-review quarantine"


def test_durable_unlink_recovery_subtrees_are_not_indexed_or_measured(
    engine,
    settings,
):
    controls = (
        (
            settings.data_dir
            / ".healthmes-recovery"
            / "unlink-recovery-v1.json"
        ),
        (
            settings.data_dir
            / "media"
            / ".healthmes-recovery"
            / "unlink-recovery-v1.json"
        ),
        (
            settings.data_dir
            / "raw_ingest"
            / ".healthmes-recovery"
            / "unlink-recovery-v1.json"
        ),
        (
            settings.data_dir
            / ".staging"
            / storage_service._DISCOVERY_CURSOR_NAME
        ),
        (
            settings.data_dir
            / ".staging"
            / storage_service._CLEANUP_JOURNAL_CURSOR_NAME
        ),
        (
            settings.data_dir
            / ".staging"
            / ".healthmes-staging-fallback-cursor-v1.json"
        ),
        (
            settings.data_dir
            / ".staging"
            / ".healthmes-staging-index-cursor-v1.json"
        ),
        (
            settings.data_dir
            / ".staging"
            / (
                ".healthmes-staging-fallback-cursor-v1.json.tmp-"
                f"{uuid.uuid4().hex}"
            )
        ),
        (
            settings.data_dir
            / ".staging"
            / (
                f"{storage_service._CLEANUP_JOURNAL_CURSOR_NAME}.tmp-"
                f"{uuid.uuid4().hex}"
            )
        ),
    )
    for control in controls:
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_bytes(b'{"internal":"durable-unlink cursor"}')

    with Session(engine) as session:
        storage_service._discover_unindexed(session, settings)
        session.commit()
        usage = storage_service.measure_usage(session, settings)
        session.flush()

        indexed_paths = set(
            session.scalars(select(StorageObject.relative_path))
        )
        assert all(
            control.relative_to(
                settings.data_dir
            ).as_posix() not in indexed_paths
            for control in controls
        )
        assert usage == {}


def test_control_like_user_payload_names_remain_indexed_and_measured(
    engine,
    settings,
):
    media_root = settings.data_dir / "media"
    media_root.mkdir(parents=True)
    names = (
        ".healthmes-recovery-photo.jpg",
        ".healthmes-unindexed-discovery-v2.json",
        ".healthmes-unindexed-discovery-v2.json-user",
        ".healthmes-staging-fallback-cursor-v1.json",
        ".healthmes-staging-fallback-cursor-v1.json-user",
        ".healthmes-staging-index-cursor-v1.json",
        ".healthmes-staging-index-cursor-v1.json-user",
        ".healthmes-storage-delete-user.jpg",
        (
            ".healthmes-storage-cleanup-v1-"
            f"{uuid.uuid4().hex}-intent.json"
        ),
        ".healthmes-unlink-not-a-journal.bin",
    )
    expected_bytes = 0
    for index, name in enumerate(names, start=1):
        payload = bytes([index]) * index
        (media_root / name).write_bytes(payload)
        expected_bytes += len(payload)

    with Session(engine) as session:
        storage_service._discover_unindexed(session, settings)
        session.commit()
        usage = storage_service.measure_usage(session, settings)
        session.flush()
        indexed_paths = set(
            session.scalars(select(StorageObject.relative_path))
        )

    assert {
        f"media/{name}" for name in names
    }.issubset(indexed_paths)
    assert usage["media"] == {
        "bytes": expected_bytes,
        "objects": len(names),
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX filename contract")
def test_unindexed_discovery_traverses_backslash_directory(
    engine,
    settings,
):
    target = (
        settings.data_dir
        / "media"
        / "directory\\name"
        / "payload\\name.bin"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(b"backslash legacy payload")

    with Session(engine) as session:
        storage_service._discover_unindexed(session, settings)
        session.commit()

    with Session(engine) as session:
        obj = session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path
                == "media/directory\\name/payload\\name.bin"
            )
        )
    assert obj is not None
    assert obj.data_class == "media"


def test_unindexed_discovery_digest_preserves_same_size_replacement(
    engine,
    settings,
):
    original = b"legacy-A"
    replacement = b"legacy-B"
    assert len(original) == len(replacement)
    target = settings.data_dir / "media" / "legacy.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    observed_at = datetime(2026, 7, 1, tzinfo=UTC)
    observed_timestamp = observed_at.timestamp()
    os.utime(target, (observed_timestamp, observed_timestamp))

    with Session(engine) as session:
        storage_service._discover_unindexed(session, settings)
        session.commit()
        obj = session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path == "media/legacy.bin"
            )
        )
        assert obj is not None
        assert obj.sha256 == hashlib.sha256(original).hexdigest()
        assert obj.safe_to_purge is True

    raced = target.with_name("legacy.replacement")
    raced.write_bytes(replacement)
    os.utime(raced, (observed_timestamp, observed_timestamp))
    os.replace(raced, target)

    with Session(engine) as session:
        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )
        obj = session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path == "media/legacy.bin"
            )
        )

        assert obj is not None
        assert obj.purged_at is None
        assert target.read_bytes() == replacement
        assert any(
            "indexed SHA-256 does not match cleanup file" in error
            for error in report.errors
        )


def test_bounded_unindexed_discovery_eventually_reaches_each_storage_class(
    engine,
    settings,
    monkeypatch,
):
    media_root = settings.data_dir / "media"
    raw_root = settings.data_dir / "raw_ingest"
    media_root.mkdir(parents=True)
    raw_root.mkdir(parents=True)
    for index in range(1025):
        (media_root / f"prefix-{index:04d}").mkdir()
        (raw_root / f"prefix-{index:04d}").mkdir()
    media_target = media_root / "zz-target.bin"
    raw_target = raw_root / "zz-target.bin"
    media_target.write_bytes(b"media payload after a large unrelated prefix")
    raw_target.write_bytes(b"raw payload after a large unrelated prefix")
    batches: dict[tuple[int, int], tuple[str, ...]] = {}

    def ordered_directory_batch(descriptor, offset):
        metadata = os.fstat(descriptor)
        identity = metadata.st_dev, metadata.st_ino
        names = batches.setdefault(
            identity,
            tuple(sorted(os.listdir(descriptor))),
        )
        batch = names[offset : offset + 64]
        next_offset = offset + len(batch)
        return batch, next_offset, next_offset >= len(names)

    monkeypatch.setattr(
        storage_service,
        "read_directory_batch",
        ordered_directory_batch,
    )

    with Session(engine) as session:
        assert storage_service._discover_unindexed(
            session,
            settings,
            max_entries=64,
        )
        session.commit()

    cursor = (
        settings.data_dir
        / ".staging"
        / storage_service._DISCOVERY_CURSOR_NAME
    )
    cursor_state = json.loads(cursor.read_text(encoding="ascii"))
    assert cursor_state["version"] == 2
    assert cursor_state["classes"]["media"][0]["offset"] == 0
    assert cursor_state["classes"]["media"][0]["batch_index"] == 32
    assert (
        cursor_state["classes"]["raw_ingest"][0]["batch_index"]
        == 32
    )
    with Session(engine) as session:
        first_paths = set(
            session.scalars(select(StorageObject.relative_path))
        )
    assert "raw_ingest/zz-target.bin" not in first_paths
    assert "media/zz-target.bin" not in first_paths

    for _attempt in range(16):
        with Session(engine) as session:
            truncated = storage_service._discover_unindexed(
                session,
                settings,
                max_entries=128,
            )
            session.commit()
        with Session(engine) as session:
            indexed = {
                row.relative_path: row.data_class
                for row in session.scalars(select(StorageObject))
            }
        if {
            "media/zz-target.bin",
            "raw_ingest/zz-target.bin",
        }.issubset(indexed):
            break
    else:
        pytest.fail("bounded discovery did not make durable eventual progress")

    assert indexed["media/zz-target.bin"] == "media"
    assert indexed["raw_ingest/zz-target.bin"] == "raw_payload"
    assert truncated is False
    assert cursor.is_file()
    assert cursor.stat().st_mode & 0o077 == 0
    assert cursor.parent.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(
    os.name == "nt",
    reason="symlink creation requires Windows privileges",
)
def test_unindexed_discovery_recovers_unsafe_and_malformed_cursor(
    engine,
    settings,
    tmp_path,
):
    target = settings.data_dir / "media" / "cursor-recovery.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cursor recovery payload")
    control = settings.data_dir / ".staging"
    control.mkdir()
    outside = tmp_path / "outside-cursor.json"
    outside.write_text("malformed external cursor", encoding="utf-8")
    cursor = control / storage_service._DISCOVERY_CURSOR_NAME
    cursor.symlink_to(outside)

    with Session(engine) as session:
        storage_service._discover_unindexed(
            session,
            settings,
            max_entries=32,
        )
        session.commit()

    assert outside.read_text(encoding="utf-8") == (
        "malformed external cursor"
    )
    assert cursor.is_file()
    assert not cursor.is_symlink()
    assert cursor.stat().st_mode & 0o077 == 0
    cursor.write_text("{malformed", encoding="ascii")
    cursor.chmod(0o600)
    raw_target = settings.data_dir / "raw_ingest" / "malformed.bin"
    raw_target.parent.mkdir()
    raw_target.write_bytes(b"malformed cursor recovery payload")
    with Session(engine) as session:
        storage_service._discover_unindexed(
            session,
            settings,
            max_entries=32,
        )
        session.commit()

    with Session(engine) as session:
        assert session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path
                == "media/cursor-recovery.bin"
            )
        ) is not None
        assert session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path
                == "raw_ingest/malformed.bin"
            )
        ) is not None


def test_unindexed_discovery_recovers_from_stale_directory_state(
    engine,
    settings,
):
    original = settings.data_dir / "media" / "aaa"
    original.mkdir(parents=True)
    target = original / "payload.bin"
    target.write_bytes(b"payload moved after cursor persistence")

    with Session(engine) as session:
        assert storage_service._discover_unindexed(
            session,
            settings,
            max_entries=1,
        )
        session.commit()

    moved = original.with_name("zzz")
    original.rename(moved)
    with Session(engine) as session:
        storage_service._discover_unindexed(
            session,
            settings,
            max_entries=16,
        )
        session.commit()

    with Session(engine) as session:
        obj = session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path == "media/zzz/payload.bin"
            )
        )
    assert obj is not None
    assert obj.data_class == "media"


def test_unindexed_discovery_hash_budget_preserves_file_and_cursor(
    engine,
    settings,
):
    payload = b"legacy payload larger than this maintenance hash slice"
    target = settings.data_dir / "media" / "budgeted-discovery.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    exhausted = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=len(payload) - 1,
        max_directory_entries=64,
    )

    with Session(engine) as session:
        with pytest.raises(MaintenanceBudgetExceeded) as raised:
            storage_service._discover_unindexed(
                session,
                settings,
                max_entries=32,
                maintenance_budget=exhausted,
            )
        assert raised.value.resource == "hash_bytes"
        assert raised.value.phase == "unindexed payload discovery hash"
        assert session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path
                == "media/budgeted-discovery.bin"
            )
        ) is None
        session.rollback()

    cursor = (
        settings.data_dir
        / ".staging"
        / storage_service._DISCOVERY_CURSOR_NAME
    )
    state = json.loads(cursor.read_text(encoding="ascii"))
    assert state["classes"]["media"][0]["batch_index"] == 0
    assert target.read_bytes() == payload

    fresh = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=len(payload),
        max_directory_entries=64,
    )
    with Session(engine) as session:
        storage_service._discover_unindexed(
            session,
            settings,
            max_entries=32,
            maintenance_budget=fresh,
        )
        session.commit()
        indexed = session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path
                == "media/budgeted-discovery.bin"
            )
        )

    assert indexed is not None
    assert indexed.sha256 == hashlib.sha256(payload).hexdigest()


def test_pending_cleanup_budget_exhaustion_precedes_new_tombstone(
    engine,
    settings,
):
    retry_payload = b"pending retry must consume the first hash allowance"
    retry_path = "media/pending-retry.bin"
    retry_target = settings.data_dir / retry_path
    retry_target.parent.mkdir(parents=True)
    retry_target.write_bytes(retry_payload)

    new_payload = b"new"
    new_path = "media/new-candidate.bin"
    new_target = settings.data_dir / new_path
    new_target.write_bytes(new_payload)
    current = datetime(2026, 8, 5, tzinfo=UTC)
    configured = settings.model_copy(
        update={
            "storage_maintenance_max_hash_bytes": len(retry_payload) - 1,
            "storage_maintenance_max_directory_entries": 4096,
        }
    )

    with Session(engine) as session:
        retry = register_storage_object(
            session,
            configured,
            relative_path=retry_path,
            data_class="media",
            content_type="application/octet-stream",
            size_bytes=len(retry_payload),
            sha256=hashlib.sha256(retry_payload).hexdigest(),
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        retry.purged_at = datetime(2026, 8, 4, tzinfo=UTC)
        retry.file_cleanup_identity = None
        new = register_storage_object(
            session,
            configured,
            relative_path=new_path,
            data_class="media",
            content_type="application/octet-stream",
            size_bytes=len(new_payload),
            sha256=hashlib.sha256(new_payload).hexdigest(),
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        retry_id = retry.id
        new_id = new.id
        session.commit()

        report = run_storage_maintenance(
            session,
            configured,
            now=current,
        )

        session.expire_all()
        stored_retry = session.get(StorageObject, retry_id)
        stored_new = session.get(StorageObject, new_id)

    assert report.budget_exhausted is True
    assert report.budget_resource == "hash_bytes"
    assert report.records_purged == 0
    assert stored_retry is not None
    assert stored_retry.purged_at is not None
    assert stored_retry.file_cleanup_identity is None
    assert stored_retry.file_cleanup_completed_at is None
    assert stored_new is not None
    assert stored_new.purged_at is None
    assert stored_new.file_cleanup_identity is None
    assert retry_target.read_bytes() == retry_payload
    assert new_target.read_bytes() == new_payload
    assert any(retry_path in error for error in report.errors)
    assert all(new_path not in error for error in report.errors)


def test_new_tombstone_hash_exhaustion_keeps_database_references_unchanged(
    engine,
    settings,
):
    payload = b"new tombstone identity must finish before DB references move"
    relative_path = "raw_ingest/2026/07/01/budgeted.bin"
    target = settings.data_dir / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    current = datetime(2026, 8, 5, tzinfo=UTC)
    configured = settings.model_copy(
        update={
            "storage_maintenance_max_hash_bytes": len(payload) - 1,
            "storage_maintenance_max_directory_entries": 4096,
        }
    )

    with Session(engine) as session:
        raw = RawIngestEvent(
            received_at=datetime(2026, 7, 1, tzinfo=UTC),
            source="budget-regression",
            content_type="application/octet-stream",
            path=relative_path,
            size_bytes=len(payload),
            sha256=digest,
            parse_status="stored_unparsed",
            forward_status="not_applicable",
            forward_detail=None,
            records_forwarded=0,
        )
        session.add(raw)
        obj = register_storage_object(
            session,
            configured,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        raw_id = raw.id
        object_id = obj.id
        session.commit()
        original_updated_at = obj.updated_at

        report = run_storage_maintenance(
            session,
            configured,
            now=current,
        )

        session.expire_all()
        stored = session.get(StorageObject, object_id)
        stored_raw = session.get(RawIngestEvent, raw_id)

    assert report.budget_exhausted is True
    assert report.budget_resource == "hash_bytes"
    assert report.budget_phase == "retention cleanup identity hash"
    assert report.records_purged == 0
    assert stored is not None
    assert stored.purged_at is None
    assert stored.file_cleanup_identity is None
    assert stored.file_cleanup_completed_at is None
    assert stored.updated_at == original_updated_at
    assert stored_raw is not None
    assert stored_raw.path == relative_path
    assert target.read_bytes() == payload


def test_cleanup_timeout_after_quarantine_is_recovered_by_fresh_run(
    engine,
    settings,
    monkeypatch,
):
    payload = b"journal and quarantine survive one interrupted cleanup"
    relative_path = "media/interrupted-cleanup.bin"
    target = settings.data_dir / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    current = datetime(2026, 8, 5, tzinfo=UTC)
    real_cleanup = storage_service._cleanup_one_named_file
    interrupted = False

    def quarantine_then_timeout(
        current_settings,
        current_path,
        expected,
        *,
        maintenance_budget=None,
    ):
        nonlocal interrupted
        if interrupted:
            return real_cleanup(
                current_settings,
                current_path,
                expected,
                maintenance_budget=maintenance_budget,
            )
        interrupted = True
        assert maintenance_budget is not None
        with storage_service._open_cleanup_parent(
            current_settings,
            current_path,
        ) as parent:
            quarantine_name = storage_service._create_cleanup_quarantine(
                parent
            )
            with storage_service._open_cleanup_quarantine(
                parent,
                quarantine_name,
            ) as quarantine:
                storage_service._quarantine_cleanup_entry(
                    parent,
                    quarantine,
                )
                entry = storage_service._cleanup_quarantine_entry(
                    quarantine
                )
                storage_service._fsync_cleanup_parent(entry)
                storage_service._fsync_cleanup_parent(parent)
        raise MaintenanceBudgetExceeded(
            resource="deadline",
            phase="injected post-quarantine timeout",
            used=11.0,
            limit=10.0,
        )

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        object_id = obj.id
        session.commit()
        monkeypatch.setattr(
            storage_service,
            "_cleanup_one_named_file",
            quarantine_then_timeout,
        )

        first = run_storage_maintenance(
            session,
            settings,
            now=current,
        )
        session.expire_all()
        pending = session.get(StorageObject, object_id)

        journal_dir = (
            settings.data_dir
            / storage_service._CLEANUP_JOURNAL_DIRECTORY
        )
        prefix = storage_service._cleanup_quarantine_prefix(target.name)
        quarantines = tuple(target.parent.glob(f"{prefix}*"))

        assert first.budget_exhausted is True
        assert first.budget_resource == "deadline"
        assert first.file_cleanup_pending == 1
        assert pending is not None
        assert pending.purged_at is not None
        assert pending.file_cleanup_identity is not None
        assert pending.file_cleanup_completed_at is None
        assert not target.exists()
        assert len(quarantines) == 1
        assert (
            quarantines[0] / storage_service._CLEANUP_QUARANTINE_ENTRY
        ).read_bytes() == payload
        assert len(
            list(journal_dir.glob(f"*{object_id.hex}-intent.json"))
        ) == 1
        assert list(
            journal_dir.glob(f"*{object_id.hex}-complete.json")
        ) == []

        monkeypatch.setattr(
            storage_service,
            "_cleanup_one_named_file",
            real_cleanup,
        )
        second = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        session.expire_all()
        completed = session.get(StorageObject, object_id)

    assert second.file_cleanup_pending == 0
    assert completed is not None
    assert completed.file_cleanup_completed_at is not None
    assert not target.exists()
    assert tuple(target.parent.glob(f"{prefix}*")) == ()
    assert list(journal_dir.glob(f"*{object_id.hex}-*.json")) == []


def test_cleanup_resumes_remaining_generation_after_deadline(
    engine,
    settings,
    monkeypatch,
):
    payload = b"two independent generations share one storage object"
    digest, relative_path, destination, staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    staged.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    staged.write_bytes(payload)
    assert destination.stat().st_ino != staged.stat().st_ino
    real_cleanup = storage_service._cleanup_one_named_file
    cleanup_budgets = []
    expired = False

    def expire_after_first_success(
        current_settings,
        current_path,
        expected,
        *,
        maintenance_budget=None,
    ):
        nonlocal expired
        cleanup_budgets.append(maintenance_budget)
        removed = real_cleanup(
            current_settings,
            current_path,
            expected,
            maintenance_budget=maintenance_budget,
        )
        if removed and not expired:
            assert maintenance_budget is not None
            maintenance_budget.deadline = 0.0
            expired = True
        return removed

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        object_id = obj.id
        session.commit()
        monkeypatch.setattr(
            storage_service,
            "_cleanup_one_named_file",
            expire_after_first_success,
        )

        first = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )
        session.expire_all()
        pending = session.get(StorageObject, object_id)

        journal_dir = (
            settings.data_dir
            / storage_service._CLEANUP_JOURNAL_DIRECTORY
        )
        assert expired is True
        assert cleanup_budgets
        assert all(budget is not None for budget in cleanup_budgets)
        assert first.files_deleted == 0
        assert first.file_cleanup_pending == 1
        assert first.bytes_reclaimed == 0
        assert first.budget_exhausted is True
        assert pending is not None
        assert pending.file_cleanup_completed_at is None
        assert sum(path.exists() for path in (destination, staged)) == 1
        assert len(
            list(journal_dir.glob(f"*{object_id.hex}-intent.json"))
        ) == 1
        progress = next(
            iter(journal_dir.glob(f"*{object_id.hex}-progress.json"))
        )
        progress_state = json.loads(progress.read_text(encoding="utf-8"))
        assert len(progress_state["completed_generations"]) == 1
        assert len(progress_state["removed_generations"]) == 1
        assert progress_state["active_generation"] is None
        assert list(
            journal_dir.glob(f"*{object_id.hex}-complete.json")
        ) == []

        monkeypatch.setattr(
            storage_service,
            "_cleanup_one_named_file",
            real_cleanup,
        )
        second = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        session.expire_all()
        completed = session.get(StorageObject, object_id)

    assert second.file_cleanup_pending == 0
    assert second.errors == ()
    assert second.files_deleted == 1
    assert second.bytes_reclaimed == 2 * len(payload)
    assert completed is not None
    assert completed.file_cleanup_completed_at is not None
    assert not destination.exists()
    assert not staged.exists()
    assert list(journal_dir.glob(f"*{object_id.hex}-*.json")) == []


def test_cleanup_resumes_after_later_generation_oserror(
    engine,
    settings,
    monkeypatch,
):
    payload = b"first generation progress survives later generation failure"
    digest, relative_path, destination, staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    staged.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    staged.write_bytes(payload)
    assert destination.stat().st_ino != staged.stat().st_ino
    real_cleanup = storage_service._cleanup_one_named_file
    calls = 0

    def fail_second_generation(
        current_settings,
        current_path,
        expected,
        *,
        maintenance_budget=None,
    ):
        nonlocal calls
        calls += 1
        assert maintenance_budget is not None
        if calls == 2:
            raise OSError("injected later generation failure")
        return real_cleanup(
            current_settings,
            current_path,
            expected,
            maintenance_budget=maintenance_budget,
        )

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        object_id = obj.id
        session.commit()
        monkeypatch.setattr(
            storage_service,
            "_cleanup_one_named_file",
            fail_second_generation,
        )

        first = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )
        session.expire_all()
        pending = session.get(StorageObject, object_id)
        journal_dir = (
            settings.data_dir
            / storage_service._CLEANUP_JOURNAL_DIRECTORY
        )
        progress = next(
            iter(journal_dir.glob(f"*{object_id.hex}-progress.json"))
        )
        progress_state = json.loads(progress.read_text(encoding="utf-8"))

        assert first.file_cleanup_pending == 1
        assert first.files_deleted == 0
        assert first.bytes_reclaimed == 0
        assert first.errors == (
            f"{relative_path}: injected later generation failure",
        )
        assert pending is not None
        assert pending.file_cleanup_completed_at is None
        assert pending.file_cleanup_identity.get(
            "manual_review_required"
        ) is None
        assert len(progress_state["completed_generations"]) == 1
        assert len(progress_state["removed_generations"]) == 1
        assert progress_state["active_generation"] is not None
        assert list(
            journal_dir.glob(f"*{object_id.hex}-manual-review.json")
        ) == []

        monkeypatch.setattr(
            storage_service,
            "_cleanup_one_named_file",
            real_cleanup,
        )
        second = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        session.expire_all()
        completed = session.get(StorageObject, object_id)

    assert second.file_cleanup_pending == 0
    assert second.files_deleted == 1
    assert second.bytes_reclaimed == 2 * len(payload)
    assert second.errors == ()
    assert completed is not None
    assert completed.file_cleanup_completed_at is not None
    assert not destination.exists()
    assert not staged.exists()
    assert list(journal_dir.glob(f"*{object_id.hex}-*.json")) == []


def test_corrupt_cleanup_journal_is_preserved_and_reported_as_pending(
    engine,
    settings,
    monkeypatch,
):
    payload = b"corrupt cleanup journal must fail closed"
    digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        object_id = obj.id
        session.commit()
        real_unlink = storage_service._unlink_cleanup_entry

        def fail_payload_unlink(parent):
            if parent.name == storage_service._CLEANUP_QUARANTINE_ENTRY:
                raise OSError("injected cleanup interruption")
            return real_unlink(parent)

        monkeypatch.setattr(
            storage_service,
            "_unlink_cleanup_entry",
            fail_payload_unlink,
        )
        first = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )
        assert first.file_cleanup_pending == 1

        journal_dir = (
            settings.data_dir
            / storage_service._CLEANUP_JOURNAL_DIRECTORY
        )
        intent = next(
            journal_dir.glob(f"*{object_id.hex}-intent.json")
        )
        corrupt_payload = b'{"not":"canonical", "truncated":'
        intent.write_bytes(corrupt_payload)

        monkeypatch.setattr(
            storage_service,
            "_unlink_cleanup_entry",
            real_unlink,
        )
        retry = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        session.refresh(obj)

        assert retry.files_deleted == 0
        assert retry.file_cleanup_pending == 1
        assert any(
            "invalid storage cleanup journal" in error
            for error in retry.errors
        )
        assert obj.file_cleanup_completed_at is None
        assert intent.read_bytes() == corrupt_payload


def test_cleanup_journal_partial_write_never_publishes_final(
    settings,
    monkeypatch,
):
    settings.data_dir.mkdir(parents=True)
    object_id = uuid.uuid4()
    payload = b'{"version":1,"state":"complete"}'
    real_write = storage_service.os.write
    calls = 0

    def interrupt_after_prefix(descriptor, remaining):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, remaining[:4])
        raise OSError("injected journal write interruption")

    with storage_service._open_cleanup_journal_parent(
        settings,
        object_id,
        create=True,
    ) as parent:
        entry = storage_service._cleanup_journal_entry(
            parent,
            object_id,
            "intent",
        )
        monkeypatch.setattr(
            storage_service.os,
            "write",
            interrupt_after_prefix,
        )
        with pytest.raises(
            OSError,
            match="journal write interruption",
        ):
            storage_service._cleanup_journal_write(entry, payload)

        assert not (entry.path / entry.name).exists()
        assert list(entry.path.glob(f"{entry.name}.tmp-*")) == []

        monkeypatch.setattr(storage_service.os, "write", real_write)
        storage_service._cleanup_journal_write(entry, payload)

        assert (entry.path / entry.name).read_bytes() == payload


def test_cleanup_journal_directory_creation_uses_shared_budget(settings):
    settings.data_dir.mkdir(parents=True)
    first_slice = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=1,
    )

    with pytest.raises(MaintenanceBudgetExceeded) as raised:
        storage_service._ensure_cleanup_journal_directory(
            settings,
            maintenance_budget=first_slice,
        )

    journal = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    assert raised.value.phase == (
        "storage cleanup journal directory creation"
    )
    assert (settings.data_dir / "raw_ingest").is_dir()
    assert not journal.exists()

    fresh_slice = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=1,
    )
    storage_service._ensure_cleanup_journal_directory(
        settings,
        maintenance_budget=fresh_slice,
    )
    assert journal.is_dir()


def test_cleanup_journal_publication_reserves_three_mutations(settings):
    settings.data_dir.mkdir(parents=True)
    object_id = uuid.uuid4()
    payload = b'{"version":1}'
    with storage_service._open_cleanup_journal_parent(
        settings,
        object_id,
        create=True,
    ) as parent:
        entry = storage_service._cleanup_journal_entry(
            parent,
            object_id,
            "intent",
        )
        too_small = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=2,
        )
        with pytest.raises(MaintenanceBudgetExceeded) as raised:
            storage_service._cleanup_journal_write(
                entry,
                payload,
                maintenance_budget=too_small,
            )
        assert raised.value.phase == "storage cleanup journal publication"
        assert not (entry.path / entry.name).exists()
        assert list(entry.path.glob(f"{entry.name}.tmp-*")) == []

        exact = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=3,
        )
        storage_service._cleanup_journal_write(
            entry,
            payload,
            maintenance_budget=exact,
        )
        assert (entry.path / entry.name).read_bytes() == payload


def test_orphan_cleanup_journal_is_preserved_and_warned(
    engine,
    settings,
):
    object_id = uuid.uuid4()
    journal_dir = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal_dir.mkdir(parents=True)
    journal = journal_dir / (
        f"{storage_service._CLEANUP_JOURNAL_PREFIX}"
        f"{object_id.hex}-intent.json"
    )
    journal_payload = b'{"orphan":true}'
    journal.write_bytes(journal_payload)

    with Session(engine) as session:
        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )

        assert any(
            "storage cleanup journal has no matching StorageObject"
            in error
            and str(object_id) in error
            for error in report.errors
        )
        assert journal.read_bytes() == journal_payload


@pytest.mark.skipif(os.name == "nt", reason="POSIX resumable scan contract")
def test_cleanup_journal_scan_cursor_eventually_reaches_every_entry(
    engine,
    settings,
    monkeypatch,
):
    journal_dir = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal_dir.mkdir(parents=True)
    object_ids = [uuid.uuid4() for _ in range(12)]
    for object_id in object_ids:
        journal = journal_dir / (
            f"{storage_service._CLEANUP_JOURNAL_PREFIX}"
            f"{object_id.hex}-intent.json"
        )
        journal.write_bytes(b'{"orphan":true}')

    batches: dict[tuple[int, int], tuple[str, ...]] = {}

    def ordered_directory_batch(descriptor, offset):
        metadata = os.fstat(descriptor)
        identity = metadata.st_dev, metadata.st_ino
        names = batches.setdefault(
            identity,
            tuple(sorted(os.listdir(descriptor))),
        )
        batch = names[offset : offset + 16]
        next_offset = offset + len(batch)
        return batch, next_offset, next_offset >= len(names)

    monkeypatch.setattr(
        storage_service,
        "_CLEANUP_JOURNAL_SCAN_LIMIT",
        4,
    )
    monkeypatch.setattr(
        storage_service,
        "read_directory_batch",
        ordered_directory_batch,
    )

    observed: set[uuid.UUID] = set()
    with Session(engine) as session:
        for _ in range(8):
            errors = storage_service._reconcile_completed_cleanup_journals(
                session,
                settings,
            )
            for object_id in object_ids:
                if any(str(object_id) in error for error in errors):
                    observed.add(object_id)
            if observed == set(object_ids):
                break

    cursor = (
        settings.data_dir
        / ".staging"
        / storage_service._CLEANUP_JOURNAL_CURSOR_NAME
    )
    assert observed == set(object_ids)
    assert cursor.is_file()
    assert stat.S_IMODE(cursor.stat().st_mode) == 0o600
    assert stat.S_IMODE(cursor.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX resumable scan contract")
def test_cleanup_journal_scan_cursor_advances_under_low_shared_budget(
    settings,
    monkeypatch,
):
    journal_dir = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal_dir.mkdir(parents=True)
    control_dir = settings.data_dir / ".staging"
    control_dir.mkdir(mode=0o700)
    names = []
    for _ in range(6):
        object_id = uuid.uuid4()
        name = (
            f"{storage_service._CLEANUP_JOURNAL_PREFIX}"
            f"{object_id.hex}-intent.json"
        )
        names.append(name)
        (journal_dir / name).write_bytes(b'{"orphan":true}')
    names.sort()

    def ordered_directory_batch(descriptor, offset):
        batch = tuple(names[offset : offset + 16])
        next_offset = offset + len(batch)
        return batch, next_offset, next_offset >= len(names)

    monkeypatch.setattr(
        storage_service,
        "_CLEANUP_JOURNAL_SCAN_LIMIT",
        len(names),
    )
    monkeypatch.setattr(
        storage_service,
        "read_directory_batch",
        ordered_directory_batch,
    )

    observed: list[str] = []
    for _ in range(len(names)):
        budget = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            # Three mutations publish the cursor; one entry remains available
            # for useful scan progress in every bounded slice.
            max_directory_entries=4,
        )
        with storage_service._open_cleanup_journal_parent(
            settings,
            uuid.UUID(int=0),
        ) as parent:
            (
                selected,
                _truncated,
                errors,
                budget_error,
            ) = storage_service._scan_cleanup_journal_names(
                settings,
                parent,
                maintenance_budget=budget,
            )
        assert errors == ()
        observed.extend(selected)
        if len(observed) < len(names):
            assert budget_error is not None
            assert budget_error.resource == "directory_entries"
        else:
            assert budget_error is None

    cursor = (
        settings.data_dir
        / ".staging"
        / storage_service._CLEANUP_JOURNAL_CURSOR_NAME
    )
    cursor_state = json.loads(cursor.read_text(encoding="ascii"))
    assert observed == names
    assert cursor_state["offset"] == 0
    assert cursor_state["batch_index"] == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX resumable scan contract")
def test_cleanup_journal_cursor_restarts_after_deleting_entries(
    engine,
    settings,
    monkeypatch,
):
    journal_dir = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal_dir.mkdir(parents=True)
    object_ids: list[uuid.UUID] = []
    completed_at = datetime(2026, 8, 18, tzinfo=UTC)

    with Session(engine) as session:
        for index in range(12):
            obj = register_storage_object(
                session,
                settings,
                relative_path=f"media/completed-{index}.bin",
                data_class="media",
                content_type="application/octet-stream",
                size_bytes=1,
                observed_at=completed_at,
            )
            obj.purged_at = completed_at
            obj.file_cleanup_identity = {
                "version": 2,
                "kind": "missing",
                "aliases": [],
            }
            obj.file_cleanup_completed_at = completed_at
            object_ids.append(obj.id)
            journal = journal_dir / (
                f"{storage_service._CLEANUP_JOURNAL_PREFIX}"
                f"{obj.id.hex}-intent.json"
            )
            journal.write_bytes(b'{"completed":true}')
        session.commit()

    def current_directory_batch(descriptor, offset):
        names = tuple(sorted(os.listdir(descriptor)))
        batch = names[offset : offset + 16]
        next_offset = offset + len(batch)
        return batch, next_offset, next_offset >= len(names)

    monkeypatch.setattr(
        storage_service,
        "_CLEANUP_JOURNAL_SCAN_LIMIT",
        4,
    )
    monkeypatch.setattr(
        storage_service,
        "read_directory_batch",
        current_directory_batch,
    )

    with Session(engine) as session:
        for _ in range(6):
            errors = storage_service._reconcile_completed_cleanup_journals(
                session,
                settings,
            )
            remaining = list(
                journal_dir.glob(
                    f"{storage_service._CLEANUP_JOURNAL_PREFIX}*.json"
                )
            )
            if not remaining:
                break
            assert any("was truncated" in error for error in errors)

    assert not remaining


def test_complete_cleanup_journal_recovers_lost_database_acknowledgement(
    engine,
    settings,
    monkeypatch,
):
    payload = b"complete journal survives lost database acknowledgement"
    digest, relative_path, destination, _staged = _payload_paths(
        settings,
        payload,
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)

    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="raw_payload",
            content_type="application/octet-stream",
            size_bytes=len(payload),
            sha256=digest,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        object_id = obj.id
        session.commit()
        real_commit = Session.commit
        commit_calls = 0

        def lose_cleanup_acknowledgement(current_session):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                raise OSError("injected cleanup acknowledgement loss")
            real_commit(current_session)

        monkeypatch.setattr(
            Session,
            "commit",
            lose_cleanup_acknowledgement,
        )
        with pytest.raises(
            OSError,
            match="cleanup acknowledgement loss",
        ):
            run_storage_maintenance(
                session,
                settings,
                now=datetime(2026, 8, 5, tzinfo=UTC),
            )

        journal_dir = (
            settings.data_dir
            / storage_service._CLEANUP_JOURNAL_DIRECTORY
        )
        assert not destination.exists()
        assert len(
            list(journal_dir.glob(f"*{object_id.hex}-intent.json"))
        ) == 1
        assert len(
            list(journal_dir.glob(f"*{object_id.hex}-complete.json"))
        ) == 1
        with Session(engine) as verification:
            stored = verification.get(StorageObject, object_id)
            assert stored is not None
            assert stored.purged_at is not None
            assert stored.file_cleanup_completed_at is None

        monkeypatch.setattr(Session, "commit", real_commit)
        retry = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 6, tzinfo=UTC),
        )
        session.refresh(obj)

        assert retry.files_deleted == 0
        assert retry.file_cleanup_pending == 0
        assert retry.errors == ()
        assert obj.file_cleanup_completed_at is not None
        assert list(journal_dir.glob(f"*{object_id.hex}-*.json")) == []


def test_cleanup_journal_and_retention_quarantine_are_not_indexed_or_measured(
    engine,
    settings,
):
    object_id = uuid.uuid4()
    journal_dir = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal_dir.mkdir(parents=True)
    journal = journal_dir / (
        f"{storage_service._CLEANUP_JOURNAL_PREFIX}"
        f"{object_id.hex}-intent.json"
    )
    journal.write_bytes(b'{"internal":"journal"}')

    quarantine = (
        settings.data_dir
        / "raw_ingest"
        / (
            storage_service._cleanup_quarantine_prefix("payload.bin")
            + uuid.uuid4().hex
        )
        / storage_service._CLEANUP_QUARANTINE_ENTRY
    )
    quarantine.parent.mkdir(parents=True)
    quarantine.write_bytes(b"internal retention quarantine")

    with Session(engine) as session:
        storage_service._discover_unindexed(session, settings)
        session.commit()
        usage = storage_service.measure_usage(session, settings)
        session.flush()

        indexed_paths = set(
            session.scalars(select(StorageObject.relative_path))
        )
        assert journal.relative_to(
            settings.data_dir
        ).as_posix() not in indexed_paths
        assert quarantine.relative_to(
            settings.data_dir
        ).as_posix() not in indexed_paths
        assert usage.get(
            "raw_payload",
            {"bytes": 0, "objects": 0},
        ) == {"bytes": 0, "objects": 0}
        assert journal.read_bytes() == b'{"internal":"journal"}'
        assert (
            quarantine.read_bytes()
            == b"internal retention quarantine"
        )


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX owner-only directory contract",
)
def test_cleanup_journal_directory_is_repaired_to_owner_only(settings):
    journal = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal.mkdir(parents=True)
    for directory in (
        settings.data_dir,
        settings.data_dir / "raw_ingest",
        journal,
    ):
        directory.chmod(0o777)

    storage_service._ensure_cleanup_journal_directory(settings)

    for directory in (
        settings.data_dir,
        settings.data_dir / "raw_ingest",
        journal,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX owner-only directory contract",
)
def test_cleanup_journal_directory_rejects_foreign_owner(
    settings,
    monkeypatch,
):
    journal = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal.mkdir(parents=True)
    real_fstat = os.fstat
    calls = 0

    def foreign_journal_owner(descriptor):
        nonlocal calls
        metadata = real_fstat(descriptor)
        calls += 1
        if calls != 3:
            return metadata
        values = list(metadata)
        values[4] = metadata.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(storage_service.os, "fstat", foreign_journal_owner)

    with pytest.raises(OSError, match="not owned by the current user"):
        storage_service._ensure_cleanup_journal_directory(settings)


def test_unknown_cleanup_journal_is_never_indexed_or_retention_deleted(
    engine,
    settings,
):
    journal_dir = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journal_dir.mkdir(parents=True)
    unknown = journal_dir / "malformed-old-journal.json"
    payload = b'{"operator_review":true}'
    unknown.write_bytes(payload)
    old_timestamp = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    os.utime(unknown, (old_timestamp, old_timestamp))
    relative = unknown.relative_to(settings.data_dir).as_posix()

    with Session(engine) as session:
        report = run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 19, tzinfo=UTC),
        )
        indexed = session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path == relative
            )
        )
        session.commit()
        usage = storage_service.measure_usage(session, settings)

    assert indexed is None
    assert unknown.read_bytes() == payload
    assert report.records_purged == 0
    assert report.files_deleted == 0
    assert any(
        "unknown storage cleanup journal entry preserved" in error
        and relative in error
        for error in report.errors
    )
    assert usage.get(
        "raw_payload",
        {"bytes": 0, "objects": 0},
    ) == {"bytes": 0, "objects": 0}


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX symlink ownership boundary",
)
def test_usage_does_not_follow_external_file_symlink(
    engine,
    settings,
    tmp_path,
):
    external = tmp_path / "external-private.bin"
    external.write_bytes(b"outside healthmes ownership")
    linked = settings.data_dir / "media" / "external-private.bin"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)

    with Session(engine) as session:
        storage_service._discover_unindexed(session, settings)
        session.commit()
        usage = storage_service.measure_usage(session, settings)
        indexed = session.scalar(
            select(StorageObject).where(
                StorageObject.relative_path
                == "media/external-private.bin"
            )
        )

    assert indexed is None
    assert usage.get(
        "media",
        {"bytes": 0, "objects": 0},
    ) == {"bytes": 0, "objects": 0}
    assert external.read_bytes() == b"outside healthmes ownership"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX symlink ownership boundary",
)
def test_usage_zeroes_existing_daily_row_after_file_becomes_symlink(
    engine,
    settings,
    tmp_path,
):
    external = tmp_path / "external-private.bin"
    external.write_bytes(b"outside healthmes ownership")
    linked = settings.data_dir / "media" / "measured-then-linked.bin"
    linked.parent.mkdir(parents=True)
    linked.write_bytes(b"owned bytes")

    with Session(engine) as session:
        assert storage_service.measure_usage(session, settings)["media"] == {
            "bytes": len(b"owned bytes"),
            "objects": 1,
        }
        session.commit()

        linked.unlink()
        linked.symlink_to(external)
        assert storage_service.measure_usage(session, settings) == {}
        row = session.scalar(
            select(StorageUsageDaily).where(
                StorageUsageDaily.measured_on == date.today(),
                StorageUsageDaily.provider == "local",
                StorageUsageDaily.data_class == "media",
            )
        )

    assert row is not None
    assert row.bytes_used == 0
    assert row.object_count == 0
    assert external.read_bytes() == b"outside healthmes ownership"


def test_usage_preserves_last_measurement_when_data_root_is_unavailable(
    engine,
    settings,
):
    payload = b"measurement must survive a temporarily unavailable data root"
    target = settings.data_dir / "media" / "payload.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    with Session(engine) as session:
        assert storage_service.measure_usage(session, settings)["media"] == {
            "bytes": len(payload),
            "objects": 1,
        }
        session.commit()

    unavailable = settings.data_dir.with_name("data-temporarily-unavailable")
    settings.data_dir.rename(unavailable)

    with Session(engine) as session:
        with pytest.raises(FileNotFoundError):
            storage_service.measure_usage(session, settings)

    with Session(engine) as session:
        row = session.scalar(
            select(StorageUsageDaily).where(
                StorageUsageDaily.measured_on == date.today(),
                StorageUsageDaily.provider == "local",
                StorageUsageDaily.data_class == "media",
            )
        )

    assert row is not None
    assert row.bytes_used == len(payload)
    assert row.object_count == 1


def test_usage_preserves_last_measurement_when_bounded_scan_expires(
    engine,
    settings,
    monkeypatch,
):
    target = settings.data_dir / "media" / "first.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"first")

    with Session(engine) as session:
        assert storage_service.measure_usage(session, settings)["media"] == {
            "bytes": 5,
            "objects": 1,
        }
        session.commit()

    (target.parent / "second.bin").write_bytes(b"second")
    monkeypatch.setattr(storage_service, "_USAGE_SCAN_ENTRY_LIMIT", 1)

    with Session(engine) as session:
        with pytest.raises(TimeoutError, match="bounded slice"):
            storage_service.measure_usage(session, settings)

    with Session(engine) as session:
        row = session.scalar(
            select(StorageUsageDaily).where(
                StorageUsageDaily.measured_on == date.today(),
                StorageUsageDaily.provider == "local",
                StorageUsageDaily.data_class == "media",
            )
        )

    assert row is not None
    assert row.bytes_used == 5
    assert row.object_count == 1


def test_usage_records_zero_for_classes_seen_only_on_an_earlier_day(
    engine,
    settings,
):
    settings.data_dir.mkdir(parents=True)
    previous_day = date.today() - timedelta(days=1)
    with Session(engine) as session:
        session.add(
            StorageUsageDaily(
                measured_on=previous_day,
                provider="local",
                data_class="media",
                bytes_used=23,
                object_count=2,
            )
        )
        session.commit()

        assert storage_service.measure_usage(session, settings) == {}
        session.commit()

    with Session(engine) as session:
        row = session.scalar(
            select(StorageUsageDaily).where(
                StorageUsageDaily.measured_on == date.today(),
                StorageUsageDaily.provider == "local",
                StorageUsageDaily.data_class == "media",
            )
        )
        assert row is not None
        assert row.bytes_used == 0
        assert row.object_count == 0


def test_usage_measurement_rejects_an_existing_caller_transaction(
    engine,
    settings,
):
    settings.data_dir.mkdir(parents=True)
    with Session(engine) as session:
        session.scalar(select(func.count()).select_from(StorageObject))

        with pytest.raises(
            RuntimeError,
            match="without an active transaction",
        ):
            storage_service.measure_usage(session, settings)

        assert session.in_transaction()


def test_usage_measurement_rejects_an_existing_connection_transaction(
    engine,
    settings,
):
    settings.data_dir.mkdir(parents=True)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection) as session:
                with pytest.raises(
                    RuntimeError,
                    match="connection without an active transaction",
                ):
                    storage_service.measure_usage(session, settings)

            assert connection.get_transaction() is transaction
            assert transaction.is_active
        finally:
            transaction.rollback()


def test_usage_measurement_rejects_mapper_specific_database_bind(
    settings,
    tmp_path,
    monkeypatch,
):
    primary = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'usage-primary.db'}"
    )
    secondary = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'usage-secondary.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.35,
    )
    Base.metadata.create_all(primary)
    Base.metadata.create_all(secondary)
    monkeypatch.setattr(
        storage_service,
        "_USAGE_SCAN_MAX_SECONDS",
        0.05,
    )

    try:
        with secondary.connect():
            with Session(
                bind=primary,
                binds={
                    StorageObject: secondary,
                    StorageUsageDaily: secondary,
                },
            ) as session:
                started = time.monotonic()
                with pytest.raises(
                    RuntimeError,
                    match="one database bind",
                ):
                    storage_service.measure_usage(session, settings)
                elapsed = time.monotonic() - started

        assert elapsed < 0.2
    finally:
        secondary.dispose()
        primary.dispose()


def test_usage_measurement_isolates_same_engine_mapper_routing(
    engine,
    settings,
):
    settings.data_dir.mkdir(parents=True)
    with Session(
        bind=engine,
        binds={
            StorageObject: engine,
            StorageUsageDaily: engine,
        },
    ) as session:
        assert storage_service.measure_usage(session, settings) == {}
        assert not session.in_transaction()


def test_usage_measurement_allows_compatible_custom_bind_routing(
    engine,
    settings,
    monkeypatch,
):
    measurement_sessions: list[Session] = []
    real_measure = storage_service._measure_usage_in_transaction

    class CompatibleCustomBindSession(Session):
        def get_bind(self, *args, **kwargs):
            return super().get_bind(*args, **kwargs)

    def observe_measurement_session(session, *args, **kwargs):
        measurement_sessions.append(session)
        assert type(session) is Session
        return real_measure(session, *args, **kwargs)

    monkeypatch.setattr(
        storage_service,
        "_measure_usage_in_transaction",
        observe_measurement_session,
    )
    settings.data_dir.mkdir(parents=True)
    with CompatibleCustomBindSession(bind=engine) as session:
        assert storage_service.measure_usage(session, settings) == {}
        assert not session.in_transaction()
        assert measurement_sessions
        assert all(
            measurement_session is not session
            for measurement_session in measurement_sessions
        )


def test_usage_measurement_ignores_divergent_custom_bind_routing(
    engine,
    settings,
    tmp_path,
):
    secondary = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'usage-custom-secondary.db'}"
    )
    Base.metadata.create_all(secondary)
    get_bind_calls = 0

    class CustomBindSession(Session):
        def get_bind(self, *args, **kwargs):
            nonlocal get_bind_calls
            get_bind_calls += 1
            mapper = kwargs.get("mapper")
            mapped_class = getattr(mapper, "class_", mapper)
            if mapped_class in {StorageObject, StorageUsageDaily}:
                return secondary
            return super().get_bind(*args, **kwargs)

    settings.data_dir.mkdir(parents=True)
    try:
        with CustomBindSession(bind=engine) as session:
            assert storage_service.measure_usage(session, settings) == {}
            assert not session.in_transaction()
            assert get_bind_calls == 0
    finally:
        secondary.dispose()


def test_usage_measurement_never_waits_for_custom_bind_routing(
    engine,
    settings,
    tmp_path,
    monkeypatch,
):
    secondary = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'usage-mapper-secondary.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.35,
    )
    Base.metadata.create_all(secondary)
    monkeypatch.setattr(
        storage_service,
        "_USAGE_SCAN_MAX_SECONDS",
        0.05,
    )
    get_bind_calls = 0

    class SlowCustomBindSession(Session):
        def get_bind(self, *args, **kwargs):
            nonlocal get_bind_calls
            get_bind_calls += 1
            time.sleep(0.35)
            return secondary

    settings.data_dir.mkdir(parents=True)
    try:
        with secondary.connect():
            with SlowCustomBindSession(bind=engine) as session:
                started = time.monotonic()
                assert storage_service.measure_usage(session, settings) == {}
                elapsed = time.monotonic() - started

        assert elapsed < 0.2
        assert get_bind_calls == 0
    finally:
        secondary.dispose()


def test_usage_measurement_bounds_write_plane_wait(
    engine,
    settings,
    monkeypatch,
):
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    measurement_finished = threading.Event()
    failures: list[BaseException] = []
    monkeypatch.setattr(
        storage_service,
        "_USAGE_SCAN_MAX_SECONDS",
        0.05,
    )

    def hold_write_plane() -> None:
        with Session(engine) as session:
            storage_service.lock_activity_write_plane(session)
            lock_acquired.set()
            assert release_lock.wait(timeout=5)
            session.rollback()

    def measure() -> None:
        with Session(engine) as session:
            try:
                storage_service.measure_usage(session, settings)
            except BaseException as exc:
                failures.append(exc)
            finally:
                measurement_finished.set()

    holder = threading.Thread(
        target=hold_write_plane,
        name="usage-lock-holder",
    )
    measurement = threading.Thread(
        target=measure,
        name="bounded-usage-measurement",
    )
    try:
        holder.start()
        assert lock_acquired.wait(timeout=5)
        measurement.start()
        assert measurement_finished.wait(timeout=1)
        assert len(failures) == 1
        assert isinstance(failures[0], TimeoutError)
        assert "bounded slice" in str(failures[0])
    finally:
        release_lock.set()
        if holder.ident is not None:
            holder.join(timeout=5)
        if measurement.ident is not None:
            measurement.join(timeout=5)


def test_usage_measurement_bounds_queue_pool_checkout(
    settings,
    tmp_path,
    monkeypatch,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'usage-pool-timeout.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        storage_service,
        "_USAGE_SCAN_MAX_SECONDS",
        0.05,
    )

    try:
        with engine.connect():
            with Session(engine) as session:
                started = time.monotonic()
                with pytest.raises(TimeoutError, match="bounded slice"):
                    storage_service.measure_usage(session, settings)
                elapsed = time.monotonic() - started

        assert 0.02 <= elapsed < 1
    finally:
        engine.dispose()


def test_usage_measurement_normalizes_queue_pool_timeout(
    settings,
    tmp_path,
    monkeypatch,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'usage-pool-error.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.02,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        storage_service,
        "_USAGE_SCAN_MAX_SECONDS",
        0.5,
    )

    try:
        with engine.connect():
            with Session(engine) as session:
                started = time.monotonic()
                with pytest.raises(TimeoutError, match="bounded slice") as caught:
                    storage_service.measure_usage(session, settings)
                elapsed = time.monotonic() - started

        assert isinstance(caught.value.__cause__, sa.exc.TimeoutError)
        assert 0.01 <= elapsed < 0.5
    finally:
        engine.dispose()


def test_usage_measurement_bounds_external_sqlite_writer_and_restores_timeout(
    engine,
    settings,
    monkeypatch,
):
    settings.data_dir.mkdir(parents=True)
    database_path = engine.url.database
    assert database_path is not None
    original_timeout_ms = 17_321
    monkeypatch.setattr(
        storage_service,
        "_USAGE_SCAN_MAX_SECONDS",
        0.15,
    )

    with engine.connect() as measurement_connection:
        storage_service.set_sqlite_busy_timeout_ms(
            measurement_connection,
            original_timeout_ms,
        )
        measurement_connection.rollback()
        with sqlite3.connect(database_path, timeout=30) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with Session(bind=measurement_connection) as session:
                started = time.monotonic()
                with pytest.raises(TimeoutError, match="bounded slice"):
                    storage_service.measure_usage(session, settings)
                elapsed = time.monotonic() - started

            assert 0.08 <= elapsed < 1
            assert (
                storage_service.sqlite_busy_timeout_ms(
                    measurement_connection
                )
                == original_timeout_ms
        )


def test_usage_measurement_leaves_caller_connection_transaction_clean(
    engine,
    settings,
):
    settings.data_dir.mkdir(parents=True)

    with engine.connect() as connection:
        assert not connection.in_transaction()
        with Session(bind=connection) as session:
            assert storage_service.measure_usage(session, settings) == {}

        assert not connection.in_transaction()
        with connection.begin():
            assert connection.scalar(sa.text("SELECT 1")) == 1
        assert not connection.in_transaction()


def test_usage_measurement_preserves_a_stricter_sqlite_busy_timeout(
    engine,
):
    original_timeout_ms = 23
    with engine.connect() as connection:
        storage_service.set_sqlite_busy_timeout_ms(
            connection,
            original_timeout_ms,
        )
        with Session(bind=connection) as session:
            try:
                storage_service._configure_usage_database_timeout(
                    session,
                    deadline=time.monotonic() + 1,
                )
                assert (
                    storage_service.sqlite_busy_timeout_ms(connection)
                    == original_timeout_ms
                )
            finally:
                session.rollback()
                storage_service._restore_usage_sqlite_busy_timeout(
                    session
                )
        assert (
            storage_service.sqlite_busy_timeout_ms(connection)
            == original_timeout_ms
        )


@pytest.mark.parametrize(
    ("sqlstate", "pgcode"),
    (
        ("55P03", None),
        ("57014", None),
        (None, "55P03"),
        (None, "57014"),
    ),
)
def test_usage_measurement_recognizes_postgres_database_timeouts(
    sqlstate,
    pgcode,
):
    original = RuntimeError("database timeout")
    original.sqlstate = sqlstate
    original.pgcode = pgcode
    error = OperationalError("SELECT storage_object", {}, original)

    assert storage_service._usage_database_timeout_error(error) is True


def test_usage_measurement_invalidates_connection_when_timeout_restore_fails(
    monkeypatch,
):
    invalidations: list[BaseException] = []

    class ConnectionStub:
        def get_transaction(self):
            return None

        def invalidate(self, cause):
            invalidations.append(cause)

    connection = ConnectionStub()
    session = SimpleNamespace(
        info={
            storage_service._USAGE_SQLITE_BUSY_TIMEOUT_INFO_KEY: (
                connection,
                17_321,
            )
        }
    )

    def fail_restore(_connection, _timeout_ms):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(
        storage_service,
        "set_sqlite_busy_timeout_ms",
        fail_restore,
    )

    storage_service._restore_usage_sqlite_busy_timeout(session)

    assert session.info == {}
    assert len(invalidations) == 1
    assert str(invalidations[0]) == "restore failed"


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_postgres_usage_measurement_database_wait_is_bounded(
    settings,
    tmp_path,
    monkeypatch,
):
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(
        database_url,
        enforce_runtime_write_fence=False,
    )
    schema = f"hm_storage_usage_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(schema)
    connect_args = {"options": f"-csearch_path={schema}"}
    engine = create_db_engine(database_url, connect_args=connect_args)
    blocker_engine = create_db_engine(
        database_url,
        connect_args=connect_args,
        enforce_runtime_write_fence=False,
    )
    scoped_settings = settings.model_copy(
        update={"data_dir": tmp_path / "postgres-usage-data"}
    )
    scoped_settings.data_dir.mkdir(parents=True)
    monkeypatch.setattr(
        storage_service,
        "_USAGE_SCAN_MAX_SECONDS",
        0.2,
    )

    try:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f"CREATE SCHEMA {quoted_schema}")
            )
        Base.metadata.create_all(engine)

        with blocker_engine.connect() as blocker:
            transaction = blocker.begin()
            blocker.execute(
                sa.text(
                    "LOCK TABLE storage_object "
                    "IN ACCESS EXCLUSIVE MODE"
                )
            )
            try:
                with Session(engine) as session:
                    started = time.monotonic()
                    with pytest.raises(
                        TimeoutError,
                        match="bounded slice",
                    ):
                        storage_service.measure_usage(
                            session,
                            scoped_settings,
                        )
                    elapsed = time.monotonic() - started
                assert 0.1 <= elapsed < 1
            finally:
                transaction.rollback()
    finally:
        blocker_engine.dispose()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"
                )
            )
        admin_engine.dispose()


def test_concurrent_usage_measurements_publish_one_serialized_snapshot(
    engine,
    settings,
    monkeypatch,
):
    payload = b"serialized storage usage"
    target = settings.data_dir / "media" / "serialized.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    first_scan_entered = threading.Event()
    release_first_scan = threading.Event()
    second_lock_attempted = threading.Event()
    second_finished = threading.Event()
    failures: list[BaseException] = []
    results: list[dict[str, dict[str, int]]] = []
    real_open = storage_service.open_directory_anchored
    real_lock = storage_service.lock_activity_write_plane

    @contextmanager
    def paused_open(root):
        if threading.current_thread().name == "usage-first":
            first_scan_entered.set()
            assert release_first_scan.wait(timeout=5)
        with real_open(root) as opened:
            yield opened

    def tracked_lock(session, **kwargs):
        if threading.current_thread().name == "usage-second":
            second_lock_attempted.set()
        return real_lock(session, **kwargs)

    monkeypatch.setattr(
        storage_service,
        "open_directory_anchored",
        paused_open,
    )
    monkeypatch.setattr(
        storage_service,
        "lock_activity_write_plane",
        tracked_lock,
    )

    def measure() -> None:
        with Session(engine) as session:
            try:
                results.append(
                    storage_service.measure_usage(session, settings)
                )
            except BaseException as exc:
                failures.append(exc)
            finally:
                if threading.current_thread().name == "usage-second":
                    second_finished.set()

    first = threading.Thread(target=measure, name="usage-first")
    second = threading.Thread(target=measure, name="usage-second")
    try:
        first.start()
        assert first_scan_entered.wait(timeout=5)
        second.start()
        assert second_lock_attempted.wait(timeout=5)
        assert not second_finished.wait(timeout=0.2)
        release_first_scan.set()
        first.join(timeout=10)
        second.join(timeout=10)

        assert not first.is_alive()
        assert not second.is_alive()
        assert failures == []
        assert results == [
            {"media": {"bytes": len(payload), "objects": 1}},
            {"media": {"bytes": len(payload), "objects": 1}},
        ]
        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(StorageUsageDaily)
                    .where(
                        StorageUsageDaily.measured_on == date.today(),
                        StorageUsageDaily.provider == "local",
                        StorageUsageDaily.data_class == "media",
                    )
                )
                == 1
            )
    finally:
        release_first_scan.set()
        if first.ident is not None:
            first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)


def test_usage_measurement_blocks_storage_writer_until_snapshot_commit(
    engine,
    settings,
    monkeypatch,
):
    settings.data_dir.mkdir(parents=True)
    target = settings.data_dir / "media" / "nutrition.jpg"
    payload = b"nutrition photo"
    scan_entered = threading.Event()
    release_scan = threading.Event()
    writer_attempted = threading.Event()
    writer_finished = threading.Event()
    failures: list[BaseException] = []
    real_open = storage_service.open_directory_anchored

    @contextmanager
    def paused_open(root):
        if threading.current_thread().name == "usage-measurement":
            scan_entered.set()
            assert release_scan.wait(timeout=5)
        with real_open(root) as opened:
            yield opened

    monkeypatch.setattr(
        storage_service,
        "open_directory_anchored",
        paused_open,
    )

    def measure() -> None:
        with Session(engine) as session:
            try:
                storage_service.measure_usage(session, settings)
            except BaseException as exc:
                failures.append(exc)

    def publish() -> None:
        with Session(engine) as session:
            try:
                writer_attempted.set()
                storage_service.lock_activity_write_plane(session)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                register_storage_object(
                    session,
                    settings,
                    relative_path="media/nutrition.jpg",
                    data_class="nutrition_media",
                    content_type="image/jpeg",
                    size_bytes=len(payload),
                    observed_at=datetime(2026, 8, 10, tzinfo=UTC),
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            finally:
                writer_finished.set()

    measurement = threading.Thread(
        target=measure,
        name="usage-measurement",
    )
    writer = threading.Thread(target=publish, name="storage-writer")
    try:
        measurement.start()
        assert scan_entered.wait(timeout=5)
        writer.start()
        assert writer_attempted.wait(timeout=5)
        assert not writer_finished.wait(timeout=0.2)
        release_scan.set()
        measurement.join(timeout=10)
        writer.join(timeout=10)

        assert not measurement.is_alive()
        assert not writer.is_alive()
        assert failures == []
        with Session(engine) as session:
            usage = storage_service.measure_usage(session, settings)
        assert usage["nutrition_media"] == {
            "bytes": len(payload),
            "objects": 1,
        }
        assert "media" not in usage
    finally:
        release_scan.set()
        if measurement.ident is not None:
            measurement.join(timeout=5)
        if writer.ident is not None:
            writer.join(timeout=5)


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX descriptor-relative traversal contract",
)
def test_usage_accepts_a_configured_alias_after_canonicalizing_the_root(
    engine,
    settings,
    tmp_path,
):
    payload = b"canonical storage alias"
    target = settings.data_dir / "media" / "alias.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    alias = tmp_path / "configured-data-alias"
    alias.symlink_to(settings.data_dir, target_is_directory=True)
    alias_settings = settings.model_copy(update={"data_dir": alias})

    with Session(engine) as session:
        usage = storage_service.measure_usage(session, alias_settings)

    assert usage["media"] == {
        "bytes": len(payload),
        "objects": 1,
    }


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX descriptor-relative traversal contract",
)
def test_usage_rejects_directory_replaced_by_symlink_after_stat(
    engine,
    settings,
    tmp_path,
    monkeypatch,
):
    media = settings.data_dir / "media"
    media.mkdir(parents=True)
    (media / "owned.bin").write_bytes(b"owned")
    outside = tmp_path / "outside-usage"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"must not be measured")
    displaced = settings.data_dir / "media-displaced"
    real_open = storage_service.os.open
    raced = False

    def replace_then_open(path, flags, *args, **kwargs):
        nonlocal raced
        if path == "media" and kwargs.get("dir_fd") is not None and not raced:
            raced = True
            media.rename(displaced)
            media.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(storage_service.os, "open", replace_then_open)
    monkeypatch.setattr(
        storage_service.os,
        "supports_dir_fd",
        {*storage_service.os.supports_dir_fd, replace_then_open},
    )

    with Session(engine) as session:
        with pytest.raises(OSError, match="safely open"):
            storage_service.measure_usage(session, settings)

    assert raced is True
    assert secret.read_bytes() == b"must not be measured"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX descriptor-relative traversal contract",
)
def test_usage_rejects_directory_generation_replaced_after_stat(
    engine,
    settings,
    monkeypatch,
):
    media = settings.data_dir / "media"
    media.mkdir(parents=True)
    (media / "owned.bin").write_bytes(b"owned")
    displaced = settings.data_dir / "media-displaced"
    real_open = storage_service.os.open
    raced = False

    def replace_then_open(path, flags, *args, **kwargs):
        nonlocal raced
        if path == "media" and kwargs.get("dir_fd") is not None and not raced:
            raced = True
            media.rename(displaced)
            media.mkdir()
            (media / "replacement.bin").write_bytes(b"replacement")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(storage_service.os, "open", replace_then_open)
    monkeypatch.setattr(
        storage_service.os,
        "supports_dir_fd",
        {*storage_service.os.supports_dir_fd, replace_then_open},
    )

    with Session(engine) as session:
        with pytest.raises(OSError, match="safely open"):
            storage_service.measure_usage(session, settings)

    assert raced is True


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX descriptor-relative traversal contract",
)
def test_usage_timeout_closes_every_open_directory_and_iterator(
    engine,
    settings,
    monkeypatch,
):
    media = settings.data_dir / "media"
    media.mkdir(parents=True)
    (media / "first.bin").write_bytes(b"first")
    real_open = storage_service.os.open
    real_close = storage_service.os.close
    real_scandir = storage_service.os.scandir
    opened: set[int] = set()
    closed: set[int] = set()
    iterators = []

    class TrackedScandir:
        def __init__(self, descriptor):
            self._iterator = real_scandir(descriptor)
            self.closed = False
            iterators.append(self)

        def __next__(self):
            return next(self._iterator)

        def close(self):
            self.closed = True
            self._iterator.close()

    def tracked_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        if descriptor in opened:
            closed.add(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(storage_service.os, "open", tracked_open)
    monkeypatch.setattr(
        storage_service.os,
        "supports_dir_fd",
        {*storage_service.os.supports_dir_fd, tracked_open},
    )
    monkeypatch.setattr(storage_service.os, "close", tracked_close)
    monkeypatch.setattr(storage_service.os, "scandir", TrackedScandir)
    monkeypatch.setattr(storage_service, "_USAGE_SCAN_ENTRY_LIMIT", 1)

    with Session(engine) as session:
        with pytest.raises(TimeoutError, match="bounded slice"):
            storage_service.measure_usage(session, settings)

    assert opened
    assert closed == opened
    assert iterators
    assert all(iterator.closed for iterator in iterators)


@pytest.mark.parametrize(
    ("purged_at", "identity", "completed_at"),
    (
        (
            None,
            {"version": 2, "kind": "missing", "aliases": []},
            None,
        ),
        (
            None,
            {"version": 2, "kind": "missing", "aliases": []},
            datetime(2026, 8, 18, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 18, tzinfo=UTC),
            None,
            datetime(2026, 8, 18, tzinfo=UTC),
        ),
    ),
)
def test_storage_object_cleanup_state_is_enforced_by_database(
    engine,
    settings,
    purged_at,
    identity,
    completed_at,
):
    with Session(engine) as session:
        obj = register_storage_object(
            session,
            settings,
            relative_path=f"media/{uuid.uuid4().hex}.bin",
            data_class="media",
            content_type="application/octet-stream",
            size_bytes=1,
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        obj.purged_at = purged_at
        obj.file_cleanup_identity = identity
        obj.file_cleanup_completed_at = completed_at
        with pytest.raises(IntegrityError):
            session.commit()
