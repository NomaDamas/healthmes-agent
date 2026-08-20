import hashlib
import os
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes import durable_files as durable_files_service
from healthmes.durable_files import DurableFileIdentity, MaintenanceBudget
from healthmes.storage import (
    index_raw_ingest,
    reconcile_staging_files,
    register_storage_object,
)
from healthmes.storage import staging as staging_service
from healthmes.store import (
    Base,
    RawIngestEvent,
    StorageObject,
    WellnessEvent,
    create_db_engine,
)


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'staging.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _create_pending_unlink_quarantine(
    parent,
    *,
    target_name: str,
    payload: bytes,
):
    target = parent / target_name
    target.write_bytes(payload)
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = parent / f".healthmes-unlink-v2-{uuid.uuid4().hex}"
    quarantine.mkdir()
    (quarantine / durable_files_service._UNLINK_METADATA_NAME).write_bytes(
        durable_files_service._unlink_metadata(
            target_name=target_name,
            expected=expected,
            expected_sha256=_sha(payload),
        )
    )
    os.rename(
        target,
        quarantine / durable_files_service._UNLINK_PAYLOAD_NAME,
    )
    return quarantine


def test_reconciler_removes_media_duplicate_proven_by_db(
    engine,
    settings,
):
    payload = b"media staging payload"
    filename = "0123456789abcdef0123456789abcdef.jpg"
    destination = settings.data_dir / "media" / "2026" / "08" / filename
    staged = settings.data_dir / ".staging" / "media" / "2026" / "08" / f"{filename}.part"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    destination.parent.mkdir(parents=True)
    os.link(staged, destination)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=f"media/2026/08/{filename}",
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()

    report = reconcile_staging_files(engine, settings)

    assert report.scanned == 1
    assert report.cleaned == 1
    assert report.restored == 0
    assert report.unresolved == 0
    assert destination.read_bytes() == payload
    assert not staged.exists()


def test_reconciler_restores_missing_raw_destination_from_db_index(
    engine,
    settings,
):
    payload = b"raw staging payload"
    filename = "041530_123456-" + _sha(payload)[:12] + ".bin"
    relative_path = f"raw_ingest/2026/08/18/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    destination = settings.data_dir / relative_path
    staged = staged.with_name(f"{filename}.part")
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    with Session(engine) as session:
        raw = RawIngestEvent(
            received_at=datetime(2026, 8, 18, 4, 15, tzinfo=UTC),
            source="reconciler-test",
            content_type="application/octet-stream",
            path=relative_path,
            size_bytes=len(payload),
            sha256=_sha(payload),
            parse_status="stored_unparsed",
            forward_status="not_applicable",
            forward_detail=None,
            records_forwarded=0,
        )
        session.add(raw)
        session.flush()
        index_raw_ingest(session, settings, raw)
        session.commit()

    report = reconcile_staging_files(engine, settings)

    assert report.scanned == 1
    assert report.cleaned == 0
    assert report.restored == 1
    assert report.unresolved == 0
    assert destination.read_bytes() == payload
    assert not staged.exists()
    with Session(engine) as session:
        assert session.scalar(select(StorageObject)) is not None
        assert session.scalar(select(WellnessEvent)) is not None


def test_reconciler_preserves_unindexed_staging_file(
    engine,
    settings,
):
    staged = settings.data_dir / ".staging" / "media" / "2026" / "08" / (
        "fedcba9876543210fedcba9876543210.jpg.part"
    )
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"not indexed yet")

    report = reconcile_staging_files(engine, settings)

    assert report.scanned == 1
    assert report.cleaned == 0
    assert report.restored == 0
    assert report.unresolved == 1
    assert staged.read_bytes() == b"not indexed yet"


def test_reconciler_preserves_conflicting_destination_generation(
    engine,
    settings,
):
    original = b"original generation"
    replacement = b"replacement generation"
    filename = "00112233445566778899aabbccddeeff.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    destination = settings.data_dir / relative_path
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(original)
    destination.write_bytes(replacement)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(original),
            sha256=_sha(original),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()

    report = reconcile_staging_files(engine, settings)

    assert report.scanned == 1
    assert report.unresolved == 1
    assert staged.read_bytes() == original
    assert destination.read_bytes() == replacement


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_reconciler_rejects_symlinked_staging_ancestor_without_external_unlink(
    engine,
    settings,
    tmp_path,
):
    payload = b"private external payload"
    filename = "11223344556677889900aabbccddeeff.jpg"
    relative_path = f"media/2026/08/{filename}"
    destination = settings.data_dir / relative_path
    external_year = tmp_path / "external-private" / "2026"
    external_stage = external_year / "08" / f"{filename}.part"
    external_stage.parent.mkdir(parents=True)
    external_stage.write_bytes(payload)
    staging_media = settings.data_dir / ".staging" / "media"
    staging_media.mkdir(parents=True)
    os.symlink(external_year, staging_media / "2026")

    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()

    report = reconcile_staging_files(engine, settings)

    assert report.cleaned == 0
    assert report.restored == 0
    assert report.unresolved >= 1
    assert any("symlink" in error.lower() for error in report.errors)
    assert external_stage.read_bytes() == payload
    assert not destination.exists()


def test_reconciler_is_bounded_and_does_not_delete_unseen_entries(
    engine,
    settings,
):
    root = settings.data_dir / ".staging" / "media" / "legacy"
    root.mkdir(parents=True)
    first = root / "first.part"
    second = root / "second.part"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=10,
    )

    assert report.truncated is True
    assert first.exists()
    assert second.exists()


def test_missing_indexed_staging_paths_consume_entry_budget(
    engine,
    settings,
):
    settings.data_dir.mkdir(parents=True)
    with Session(engine) as session:
        for index in range(6):
            filename = f"{index:032x}.jpg"
            register_storage_object(
                session,
                settings,
                relative_path=f"media/2026/08/{filename}",
                data_class="media",
                content_type="image/jpeg",
                size_bytes=1,
                sha256=_sha(bytes([index])),
                observed_at=datetime(2026, 8, 18, tzinfo=UTC),
            )
        session.commit()

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=5,
        max_seconds=10,
    )

    assert report.scanned == 3
    assert report.cleaned == 0
    assert report.restored == 0
    assert report.truncated is True


def test_missing_indexed_staging_paths_stop_at_time_budget(
    engine,
    settings,
    monkeypatch,
):
    settings.data_dir.mkdir(parents=True)
    with Session(engine) as session:
        for index in range(3):
            filename = f"{index + 16:032x}.jpg"
            register_storage_object(
                session,
                settings,
                relative_path=f"media/2026/08/{filename}",
                data_class="media",
                content_type="image/jpeg",
                size_bytes=1,
                sha256=_sha(bytes([index])),
                observed_at=datetime(2026, 8, 18, tzinfo=UTC),
            )
        session.commit()

    now = [0.0]
    monkeypatch.setattr(
        staging_service.time,
        "monotonic",
        lambda: now[0],
    )
    original_metadata = staging_service._indexed_stage_metadata
    inspections = 0

    def advancing_metadata(*args, **kwargs):
        nonlocal inspections
        result = original_metadata(*args, **kwargs)
        inspections += 1
        now[0] = 2.0
        return result

    monkeypatch.setattr(
        staging_service,
        "_indexed_stage_metadata",
        advancing_metadata,
    )

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=10,
        max_seconds=1,
    )

    assert report.scanned == 1
    assert report.cleaned == 0
    assert report.restored == 0
    assert report.truncated is True
    assert inspections == 1


def test_staging_tree_scan_resumes_at_entry_budget(
    engine,
    settings,
):
    root = settings.data_dir / ".staging" / "media"
    root.mkdir(parents=True)
    for index in range(100):
        (root / f"{index:04d}.part").write_bytes(b"preserve")

    indexed_turn = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=10,
    )
    fallback_turn = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=10,
    )

    assert indexed_turn.scanned == 0
    assert indexed_turn.truncated is True
    assert fallback_turn.scanned == 1
    assert fallback_turn.unresolved == 1
    assert fallback_turn.truncated is True
    assert len(list(root.iterdir())) == 100
    cursor = (
        settings.data_dir
        / ".staging"
        / staging_service._FALLBACK_CURSOR_NAME
    )
    assert cursor.is_file()
    state = staging_service._decode_fallback_state(cursor.read_bytes())
    assert state.stacks["media"][-1].batch_index == 1


def test_single_entry_turns_advance_both_fallback_roots(
    engine,
    settings,
):
    media = settings.data_dir / ".staging" / "media" / "legacy.part"
    raw = settings.data_dir / ".staging" / "raw_ingest" / "legacy.part"
    media.parent.mkdir(parents=True)
    raw.parent.mkdir(parents=True)
    media.write_bytes(b"media")
    raw.write_bytes(b"raw")

    reports = [
        reconcile_staging_files(
            engine,
            settings,
            max_entries=1,
            max_seconds=10,
        )
        for _ in range(4)
    ]

    assert [report.scanned for report in reports] == [0, 1, 0, 1]
    assert str(media) in reports[1].errors[0]
    assert str(raw) in reports[3].errors[0]
    assert media.read_bytes() == b"media"
    assert raw.read_bytes() == b"raw"


def test_completed_fallback_root_is_rearmed_while_other_root_is_large(
    settings,
):
    media_root = settings.data_dir / ".staging" / "media"
    raw_root = settings.data_dir / ".staging" / "raw_ingest"
    media_root.mkdir(parents=True)
    raw_root.mkdir(parents=True)
    for index in range(200):
        (raw_root / f"{index:04d}.part").write_bytes(b"raw")

    with staging_service._open_data_root(settings) as root_descriptor:
        first = staging_service._scan_staging(
            settings,
            root_descriptor,
            max_entries=32,
            deadline=time.monotonic() + 10,
            excluded=set(),
        )

    assert first.consumed == 32
    cursor = (
        settings.data_dir
        / ".staging"
        / staging_service._FALLBACK_CURSOR_NAME
    )
    state = staging_service._decode_fallback_state(cursor.read_bytes())
    assert state.stacks["media"] == []
    assert state.stacks["raw_ingest"]

    filename = "abababababababababababababababab.jpg"
    newly_staged = media_root / "2026" / "08" / f"{filename}.part"
    newly_staged.parent.mkdir(parents=True)
    newly_staged.write_bytes(b"new media")

    with staging_service._open_data_root(settings) as root_descriptor:
        second = staging_service._scan_staging(
            settings,
            root_descriptor,
            max_entries=32,
            deadline=time.monotonic() + 10,
            excluded=set(),
        )

    assert any(
        item.candidate is not None
        and item.candidate.staged == newly_staged
        for item in second.items
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX owner-only cursor and no-follow contract",
)
def test_staging_fallback_cursor_rejects_symlink_and_rewrites_private(
    settings,
    tmp_path,
):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    control = settings.data_dir / ".staging"
    control.mkdir(mode=0o755)
    outside = tmp_path / "outside-fallback-cursor.json"
    outside.write_text('{"external":true}', encoding="ascii")
    cursor = control / staging_service._FALLBACK_CURSOR_NAME
    cursor.symlink_to(outside)

    with staging_service._open_data_root(settings) as root_descriptor:
        result = staging_service._scan_staging(
            settings,
            root_descriptor,
            max_entries=1,
            deadline=time.monotonic() + 10,
            excluded=set(),
        )

    assert any(
        item.error is not None
        and "invalid staging fallback cursor" in item.error
        for item in result.items
    )
    assert outside.read_text(encoding="ascii") == '{"external":true}'
    assert cursor.is_file()
    assert not cursor.is_symlink()
    assert cursor.stat().st_mode & 0o077 == 0
    assert control.stat().st_mode & 0o077 == 0


def test_fallback_cursor_write_failure_preserves_scan_report(
    engine,
    settings,
    monkeypatch,
):
    staged = (
        settings.data_dir
        / ".staging"
        / "media"
        / "legacy"
        / "preserve.part"
    )
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"preserve")

    def fail_cursor_write(*_args, **_kwargs):
        raise OSError("simulated cursor fsync failure")

    monkeypatch.setattr(
        staging_service,
        "_write_fallback_state",
        fail_cursor_write,
    )

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=2,
        max_seconds=10,
    )

    assert report.scanned == 1
    assert report.unresolved == 2
    assert report.truncated is True
    assert any("unmapped staging file preserved" in error for error in report.errors)
    assert any(
        "could not persist staging fallback cursor" in error
        and "simulated cursor fsync failure" in error
        for error in report.errors
    )
    assert staged.read_bytes() == b"preserve"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX no-follow staging namespace contract",
)
def test_fallback_namespace_failure_is_not_reported_as_cursor_write(
    engine,
    settings,
    tmp_path,
):
    settings.data_dir.mkdir(parents=True)
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    (settings.data_dir / ".staging").symlink_to(
        outside,
        target_is_directory=True,
    )

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=2,
        max_seconds=10,
    )

    assert report.unresolved >= 1
    assert report.truncated is True
    assert any(
        "could not scan staging fallback namespace" in error
        for error in report.errors
    )
    assert not any(
        "could not persist staging fallback cursor" in error
        for error in report.errors
    )
    assert list(outside.iterdir()) == []


def test_reconciler_exact_budget_reports_unprobed_tree_as_truncated(
    engine,
    settings,
):
    payload = b"one exact indexed staging payload"
    filename = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    destination = settings.data_dir / relative_path
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=10,
    )

    assert report.scanned == 1
    assert report.cleaned == 1
    assert report.truncated is True
    assert destination.read_bytes() == payload
    assert not staged.exists()


def test_indexed_candidate_is_not_starved_by_earlier_junk(
    engine,
    settings,
):
    junk_root = settings.data_dir / ".staging" / "media" / "legacy"
    junk_root.mkdir(parents=True)
    for index in range(300):
        (junk_root / f"{index:04d}.part").write_bytes(b"junk")

    payload = b"indexed payload behind junk"
    filename = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    destination = settings.data_dir / relative_path
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=10,
    )

    assert report.scanned == 1
    assert report.cleaned == 1
    assert report.truncated is True
    assert destination.read_bytes() == payload
    assert not staged.exists()
    assert len(list(junk_root.iterdir())) == 300


def test_indexed_cursor_reaches_staged_payload_after_missing_paths(
    engine,
    settings,
    monkeypatch,
):
    payload = b"staged payload after one full missing-path page"
    filename = "ffffffffffffffffffffffffffffffff.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    destination = settings.data_dir / relative_path
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)

    observed_at = datetime(2026, 8, 18, tzinfo=UTC)
    with Session(engine) as session:
        for index in range(256):
            missing_name = f"{index:032x}.jpg"
            session.add(
                StorageObject(
                    id=uuid.UUID(int=index + 1),
                    data_class="media",
                    relative_path=f"media/2026/08/{missing_name}",
                    content_type="image/jpeg",
                    size_bytes=1,
                    sha256=_sha(bytes([index % 256])),
                    retention_basis_at=observed_at,
                    safe_to_purge=True,
                )
            )
        session.add(
            StorageObject(
                id=uuid.UUID(int=257),
                data_class="media",
                relative_path=relative_path,
                content_type="image/jpeg",
                size_bytes=len(payload),
                sha256=_sha(payload),
                retention_basis_at=observed_at,
                safe_to_purge=True,
            )
        )
        session.commit()

    monkeypatch.setattr(
        staging_service,
        "_scan_staging",
        lambda *_args, **_kwargs: staging_service._FallbackScanResult(
            items=(),
            consumed=0,
            truncated=False,
        ),
    )

    first = reconcile_staging_files(
        engine,
        settings,
        max_entries=256,
        max_seconds=10,
    )
    second = reconcile_staging_files(
        engine,
        settings,
        max_entries=256,
        max_seconds=10,
    )

    assert first.scanned == 192
    assert first.restored == 0
    assert first.truncated is True
    assert second.restored == 1
    assert destination.read_bytes() == payload
    assert not staged.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX owner-only cursor and no-follow contract",
)
def test_staging_index_cursor_rejects_symlink_and_rewrites_private(
    settings,
    tmp_path,
):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    control = settings.data_dir / ".staging"
    control.mkdir(mode=0o755)
    outside = tmp_path / "outside-staging-cursor.json"
    outside.write_text('{"external":true}', encoding="ascii")
    cursor = control / staging_service._INDEX_CURSOR_NAME
    cursor.symlink_to(outside)
    expected = uuid.uuid4()

    with staging_service._open_data_root(settings) as root_descriptor:
        value, error = staging_service._read_index_cursor(
            settings,
            root_descriptor,
        )
        assert value.storage_object_id is None
        assert error is not None
        assert "invalid staging index cursor" in error
        staging_service._write_index_cursor(
            settings,
            root_descriptor,
            staging_service._IndexCursorState(
                storage_object_id=expected,
                next_pass="fallback",
            ),
        )

    assert outside.read_text(encoding="ascii") == '{"external":true}'
    assert cursor.is_file()
    assert not cursor.is_symlink()
    assert cursor.stat().st_mode & 0o077 == 0
    assert control.stat().st_mode & 0o077 == 0


def test_index_cursor_write_failure_fails_closed_then_resumes_later_rows(
    engine,
    settings,
    monkeypatch,
):
    observed_at = datetime(2026, 8, 18, tzinfo=UTC)
    payloads = (b"first indexed payload", b"second indexed payload")
    paths = []
    with Session(engine) as session:
        for index, payload in enumerate(payloads, start=1):
            filename = f"{index:032x}.jpg"
            relative_path = f"media/2026/08/{filename}"
            staged = settings.data_dir / ".staging" / relative_path
            staged = staged.with_name(f"{filename}.part")
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(payload)
            session.add(
                StorageObject(
                    id=uuid.UUID(int=index),
                    data_class="media",
                    relative_path=relative_path,
                    content_type="image/jpeg",
                    size_bytes=len(payload),
                    sha256=_sha(payload),
                    retention_basis_at=observed_at,
                    safe_to_purge=True,
                )
            )
            paths.append((staged, settings.data_dir / relative_path))
        session.commit()

    monkeypatch.setattr(
        staging_service,
        "_scan_staging",
        lambda *_args, **_kwargs: staging_service._FallbackScanResult(
            items=(),
            consumed=0,
            truncated=False,
        ),
    )
    real_write = staging_service._write_index_cursor
    writes = 0

    def fail_first_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("simulated index cursor fsync failure")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(
        staging_service,
        "_write_index_cursor",
        fail_first_write,
    )

    with pytest.raises(
        OSError,
        match=(
            "could not persist staging index cursor.*"
            "simulated index cursor fsync failure"
        ),
    ):
        reconcile_staging_files(
            engine,
            settings,
            max_entries=2,
            max_seconds=10,
        )

    assert paths[0][1].read_bytes() == payloads[0]
    assert not paths[0][0].exists()
    assert paths[1][0].read_bytes() == payloads[1]

    repeated = reconcile_staging_files(
        engine,
        settings,
        max_entries=2,
        max_seconds=10,
    )
    resumed = reconcile_staging_files(
        engine,
        settings,
        max_entries=2,
        max_seconds=10,
    )

    assert repeated.scanned == 1
    assert resumed.restored == 1
    assert paths[1][1].read_bytes() == payloads[1]
    assert not paths[1][0].exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable-unlink recovery")
def test_full_index_page_does_not_starve_pending_unlink_quarantine(
    engine,
    settings,
    monkeypatch,
):
    settings.data_dir.mkdir(parents=True)
    quarantine = _create_pending_unlink_quarantine(
        settings.data_dir,
        target_name="crash-left.bin",
        payload=b"crash-left durable unlink",
    )
    observed_at = datetime(2026, 8, 18, tzinfo=UTC)
    with Session(engine) as session:
        for index in range(256):
            session.add(
                StorageObject(
                    id=uuid.UUID(int=index + 1),
                    data_class="media",
                    relative_path=(
                        f"media/2026/08/{index:032x}.jpg"
                    ),
                    content_type="image/jpeg",
                    size_bytes=1,
                    sha256=_sha(bytes([index % 256])),
                    retention_basis_at=observed_at,
                    safe_to_purge=True,
                )
            )
        session.commit()

    real_recover = staging_service.recover_durable_unlink_quarantines
    recovery_budgets = []

    def record_recovery_budget(*args, **kwargs):
        recovery_budgets.append(kwargs["max_entries"])
        return real_recover(*args, **kwargs)

    monkeypatch.setattr(
        staging_service,
        "recover_durable_unlink_quarantines",
        record_recovery_budget,
    )

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=256,
        max_seconds=10,
    )

    assert report.scanned == 192
    assert report.restored == 0
    assert report.cleaned == 0
    assert 1 <= report.unlink_quarantines_scanned <= 256
    assert report.unlink_quarantines_cleaned == 1
    assert report.unresolved == 0
    assert report.truncated is True
    assert recovery_budgets == [256]
    assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable-unlink recovery")
def test_staging_recovery_uses_and_resumes_the_shared_lifecycle_budget(
    engine,
    settings,
):
    settings.data_dir.mkdir(parents=True)
    payload = b"shared staging lifecycle budget"
    quarantine = _create_pending_unlink_quarantine(
        settings.data_dir,
        target_name="shared-budget.bin",
        payload=payload,
    )
    exhausted = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=64,
    )

    first = reconcile_staging_files(
        engine,
        settings,
        max_entries=8,
        max_seconds=10,
        maintenance_budget=exhausted,
    )

    assert first.budget_exhausted is True
    assert first.truncated is True
    assert first.unlink_quarantines_scanned == 1
    assert first.unlink_quarantines_cleaned == 0
    assert quarantine.exists()
    assert any("hash_bytes" in error for error in first.errors)

    fresh = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=len(payload),
        max_directory_entries=64,
    )
    resumed = reconcile_staging_files(
        engine,
        settings,
        max_entries=8,
        max_seconds=10,
        maintenance_budget=fresh,
    )

    assert resumed.budget_exhausted is False
    assert resumed.unlink_quarantines_cleaned == 1
    assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor contract")
def test_staging_restore_charges_hashes_and_namespace_mutations(
    engine,
    settings,
):
    payload = b"restore budget accounting"
    filename = "cccccccccccccccccccccccccccccccc.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()
        budget = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=2 * len(payload),
            max_directory_entries=5,
        )
        candidate = staging_service._indexed_candidate(
            settings,
            relative_path,
        )
        assert candidate is not None
        with staging_service._open_data_root(settings) as root_descriptor:
            outcome = staging_service._reconcile_candidate(
                session,
                settings,
                root_descriptor,
                candidate,
                deadline=time.monotonic() + 10,
                maintenance_budget=budget,
            )

    assert outcome == "restored"
    assert budget._remaining_hash_bytes == 0
    assert budget._remaining_directory_entries == 0
    assert (settings.data_dir / relative_path).read_bytes() == payload
    assert not staged.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor contract")
def test_staging_duplicate_reuses_same_inode_digest_and_charges_unlink(
    engine,
    settings,
):
    payload = b"same inode digest cache"
    filename = "dddddddddddddddddddddddddddddddd.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    destination = settings.data_dir / relative_path
    staged.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    os.link(staged, destination)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()
        budget = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=len(payload),
            max_directory_entries=1,
        )
        candidate = staging_service._indexed_candidate(
            settings,
            relative_path,
        )
        assert candidate is not None
        with staging_service._open_data_root(settings) as root_descriptor:
            outcome = staging_service._reconcile_candidate(
                session,
                settings,
                root_descriptor,
                candidate,
                deadline=time.monotonic() + 10,
                maintenance_budget=budget,
            )

    assert outcome == "cleaned"
    assert budget._remaining_hash_bytes == 0
    assert budget._remaining_directory_entries == 0
    assert destination.read_bytes() == payload
    assert not staged.exists()


def test_indexed_and_fallback_scans_share_one_lifecycle_budget(
    engine,
    settings,
    monkeypatch,
):
    settings.data_dir.mkdir(parents=True)
    filename = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee.jpg"
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=f"media/2026/08/{filename}",
            data_class="media",
            content_type="image/jpeg",
            size_bytes=1,
            sha256=_sha(b"x"),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()
    seen_budget_ids = []

    def no_unlink_recovery(*_args, **_kwargs):
        return durable_files_service.DurableUnlinkRecoveryReport(
            scanned=0,
            restored=0,
            cleaned=0,
            unresolved=0,
            truncated=False,
            errors=(),
        )

    def one_fallback_scan(*_args, **kwargs):
        budget = kwargs["maintenance_budget"]
        seen_budget_ids.append(id(budget))
        budget.consume_directory_entry(
            phase="staging fallback scan",
            operation="scan",
        )
        return staging_service._FallbackScanResult(
            items=(),
            consumed=1,
            truncated=True,
        )

    monkeypatch.setattr(
        staging_service,
        "recover_durable_unlink_quarantines",
        no_unlink_recovery,
    )
    monkeypatch.setattr(
        staging_service,
        "_scan_staging",
        one_fallback_scan,
    )
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=16,
    )

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=2,
        max_seconds=10,
        maintenance_budget=budget,
    )

    assert report.scanned == 1
    assert seen_budget_ids == [id(budget)]
    # New .staging index cursor (4), indexed scan (1), fallback scan (1).
    assert budget._remaining_directory_entries == 10


def test_recovery_budget_exhaustion_skips_index_and_fallback(
    engine,
    settings,
    monkeypatch,
):
    calls = []

    def exhausted_recovery(*_args, **_kwargs):
        return durable_files_service.DurableUnlinkRecoveryReport(
            scanned=0,
            restored=0,
            cleaned=0,
            unresolved=0,
            truncated=True,
            errors=("shared recovery budget exhausted",),
            budget_exhausted=True,
        )

    def unexpected_index(*_args, **_kwargs):
        calls.append("index")
        return iter(())

    def unexpected_fallback(*_args, **_kwargs):
        calls.append("fallback")
        return staging_service._FallbackScanResult(
            items=(),
            consumed=0,
            truncated=False,
        )

    monkeypatch.setattr(
        staging_service,
        "recover_durable_unlink_quarantines",
        exhausted_recovery,
    )
    monkeypatch.setattr(
        staging_service,
        "_indexed_staging_candidates",
        unexpected_index,
    )
    monkeypatch.setattr(
        staging_service,
        "_scan_staging",
        unexpected_fallback,
    )

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=8,
        max_seconds=10,
        maintenance_budget=MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=64,
        ),
    )

    assert report.budget_exhausted is True
    assert report.truncated is True
    assert calls == []
    assert report.errors == ("shared recovery budget exhausted",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX cursor contract")
def test_staging_index_cursor_reserves_exact_mutation_capsule(settings):
    settings.data_dir.mkdir(parents=True)
    with staging_service._open_data_root(settings) as root_descriptor:
        missing = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=4,
        )
        staging_service._write_index_cursor(
            settings,
            root_descriptor,
            staging_service._IndexCursorState(
                storage_object_id=None,
                next_pass="indexed",
            ),
            maintenance_budget=missing,
        )
        assert missing._remaining_directory_entries == 0

        existing = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=3,
        )
        staging_service._write_index_cursor(
            settings,
            root_descriptor,
            staging_service._IndexCursorState(
                storage_object_id=None,
                next_pass="fallback",
            ),
            maintenance_budget=existing,
        )
        assert existing._remaining_directory_entries == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX cursor contract")
def test_fallback_cursor_does_not_advance_past_budget_failure(settings):
    staged = (
        settings.data_dir
        / ".staging"
        / "media"
        / "preserve.part"
    )
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"preserve")
    exhausted = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=3,
    )
    with staging_service._open_data_root(settings) as root_descriptor:
        first = staging_service._scan_staging(
            settings,
            root_descriptor,
            max_entries=1,
            deadline=time.monotonic() + 10,
            excluded=set(),
            maintenance_budget=exhausted,
        )

    cursor = (
        settings.data_dir
        / ".staging"
        / staging_service._FALLBACK_CURSOR_NAME
    )
    state = staging_service._decode_fallback_state(cursor.read_bytes())
    assert first.budget_error is not None
    assert first.consumed == 0
    assert state.stacks["media"][-1].batch_index == 0

    fresh = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=4,
    )
    with staging_service._open_data_root(settings) as root_descriptor:
        resumed = staging_service._scan_staging(
            settings,
            root_descriptor,
            max_entries=1,
            deadline=time.monotonic() + 10,
            excluded=set(),
            maintenance_budget=fresh,
        )

    assert resumed.budget_error is None
    assert resumed.consumed == 1
    assert any(
        item.error is not None
        and "unmapped staging file preserved" in item.error
        for item in resumed.items
    )


def test_index_cursor_does_not_skip_hash_budget_failure(
    engine,
    settings,
    monkeypatch,
):
    payload = b"retry this exact indexed candidate"
    filename = "ffffffffffffffffffffffffffffffff.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()

    monkeypatch.setattr(
        staging_service,
        "recover_durable_unlink_quarantines",
        lambda *_args, **_kwargs: (
            durable_files_service.DurableUnlinkRecoveryReport(
                scanned=0,
                restored=0,
                cleaned=0,
                unresolved=0,
                truncated=False,
                errors=(),
            )
        ),
    )
    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=2,
        max_seconds=10,
        maintenance_budget=MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=32,
        ),
    )

    cursor = (
        settings.data_dir
        / ".staging"
        / staging_service._INDEX_CURSOR_NAME
    )
    with staging_service._open_data_root(settings) as root_descriptor:
        state, error = staging_service._read_index_cursor(
            settings,
            root_descriptor,
        )
    assert error is None
    assert report.budget_exhausted is True
    assert state.storage_object_id is None
    assert cursor.is_file()
    assert staged.read_bytes() == payload


def test_staging_hash_stops_at_deadline_without_publishing(
    engine,
    settings,
    monkeypatch,
):
    payload = b"x" * (3 * 1024 * 1024)
    filename = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    destination = settings.data_dir / relative_path
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(payload),
            sha256=_sha(payload),
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        session.commit()

    real_read = staging_service.os.read
    payload_reads = 0

    def slow_read(descriptor, size):
        nonlocal payload_reads
        if size == 1024 * 1024:
            payload_reads += 1
            time.sleep(0.03)
        return real_read(descriptor, size)

    monkeypatch.setattr(staging_service.os, "read", slow_read)

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=0.02,
    )

    assert report.restored == 0
    assert report.truncated is True
    assert payload_reads == 1
    assert staged.read_bytes() == payload
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race contract")
def test_same_inode_rewrite_after_final_hash_is_preserved_for_review(
    engine,
    settings,
    monkeypatch,
):
    original = b"verified payload"
    replacement = b"rewritten data!!"
    assert len(original) == len(replacement)
    filename = "abababababababababababababababab.jpg"
    relative_path = f"media/2026/08/{filename}"
    staged = settings.data_dir / ".staging" / relative_path
    staged = staged.with_name(f"{filename}.part")
    destination = settings.data_dir / relative_path
    staged.parent.mkdir(parents=True)
    staged.write_bytes(original)
    with Session(engine) as session:
        register_storage_object(
            session,
            settings,
            relative_path=relative_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=len(original),
            sha256=_sha(original),
            observed_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
        session.commit()

    real_revalidate = staging_service._revalidate_verified_entry
    revalidations = 0
    rewrite_done = False

    def rewrite_after_destination_hash(*args, **kwargs):
        nonlocal revalidations, rewrite_done
        result = real_revalidate(*args, **kwargs)
        revalidations += 1
        if revalidations == 3 and not rewrite_done:
            rewrite_done = True
            staged.unlink()
            staged.write_bytes(replacement)
        return result

    monkeypatch.setattr(
        staging_service,
        "_revalidate_verified_entry",
        rewrite_after_destination_hash,
    )

    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=2,
        max_seconds=10,
    )

    assert rewrite_done is True
    assert report.restored == 0
    assert report.unresolved == 1
    assert any("generation changed" in error for error in report.errors)
    assert staged.read_bytes() == replacement
    assert destination.read_bytes() == original


def test_low_shared_budget_eventually_runs_fallback_turn(
    engine,
    settings,
    monkeypatch,
):
    staged = settings.data_dir / ".staging" / "media" / "legacy.part"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"preserve")
    monkeypatch.setattr(
        staging_service,
        "recover_durable_unlink_quarantines",
        lambda *_args, **_kwargs: (
            durable_files_service.DurableUnlinkRecoveryReport(
                scanned=0,
                restored=0,
                cleaned=0,
                unresolved=0,
                truncated=False,
                errors=(),
            )
        ),
    )

    first = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=10,
        maintenance_budget=MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=4,
        ),
    )
    second = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=10,
        maintenance_budget=MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=0,
            max_directory_entries=4,
        ),
    )

    assert first.scanned == 0
    assert second.scanned == 1
    assert second.budget_exhausted is False
    assert any(
        "unmapped staging file preserved" in error
        for error in second.errors
    )
    assert staged.read_bytes() == b"preserve"


@pytest.mark.parametrize("cursor_kind", ["index", "fallback"])
def test_cursor_read_honors_reconciliation_deadline(
    engine,
    settings,
    monkeypatch,
    cursor_kind,
):
    control = settings.data_dir / ".staging"
    control.mkdir(parents=True)
    with staging_service._open_data_root(settings) as root_descriptor:
        if cursor_kind == "index":
            staging_service._write_index_cursor(
                settings,
                root_descriptor,
                staging_service._IndexCursorState(
                    storage_object_id=None,
                    next_pass="indexed",
                ),
            )
            cursor_name = staging_service._INDEX_CURSOR_NAME
        else:
            with staging_service._open_relative_directory(
                root_descriptor,
                settings.data_dir,
                (".staging",),
            ) as parent:
                staging_service._write_fallback_state(
                    parent,
                    staging_service._fresh_fallback_state(),
                )
            cursor_name = staging_service._FALLBACK_CURSOR_NAME

    real_read = staging_service.os.read

    def slow_cursor_read(descriptor, size):
        path = os.readlink(f"/dev/fd/{descriptor}")
        if path.endswith(cursor_name):
            time.sleep(0.03)
        return real_read(descriptor, size)

    monkeypatch.setattr(staging_service.os, "read", slow_cursor_read)

    started = time.monotonic()
    report = reconcile_staging_files(
        engine,
        settings,
        max_entries=1,
        max_seconds=0.01,
    )

    assert time.monotonic() - started < 0.2
    assert report.truncated is True
    assert report.scanned == 0


def test_crash_left_cursor_temporaries_are_cleaned_and_names_are_strict(
    engine,
    settings,
):
    control = settings.data_dir / ".staging"
    media = control / "media"
    media.mkdir(parents=True)
    prefix = staging_service._INDEX_CURSOR_NAME
    temporary = control / f"{prefix}.tmp-{uuid.uuid4().hex}"
    malformed = control / f"{prefix}.tmp-not-a-uuid"
    unsafe = control / (
        f"{staging_service._FALLBACK_CURSOR_NAME}.tmp-{uuid.uuid4().hex}"
    )
    temporary.write_bytes(b"crash-left")
    malformed.write_bytes(b"keep")
    unsafe.mkdir()

    reports = [
        reconcile_staging_files(
            engine,
            settings,
            max_entries=2,
            max_seconds=10,
        )
        for _ in range(8)
    ]

    assert not temporary.exists()
    assert malformed.read_bytes() == b"keep"
    assert unsafe.is_dir()
    assert any(
        "unsafe staging cursor temporary preserved" in error
        for report in reports
        for error in report.errors
    )


@pytest.mark.parametrize(
    "max_seconds",
    [True, float("nan"), float("inf"), float("-inf"), 0, -1],
)
def test_reconcile_rejects_non_finite_or_non_positive_max_seconds(
    engine,
    settings,
    max_seconds,
):
    with pytest.raises(
        ValueError,
        match="max_seconds must be a finite positive number",
    ):
        reconcile_staging_files(
            engine,
            settings,
            max_seconds=max_seconds,
        )
