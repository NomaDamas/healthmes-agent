"""Sync-state / journal store tests: round-trips, per-source isolation, corruption."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from healthmes.calendars.state import (
    FilePendingDiffStore,
    FileSyncHealthStore,
    FileSyncStateStore,
    InMemoryPendingDiffStore,
    InMemorySyncHealthStore,
    InMemorySyncStateStore,
    PendingDiffStore,
    SyncCoverageKind,
    SyncHealthStatus,
    SyncHealthStore,
    SyncStateStore,
    sync_state_coverage,
    with_sync_state_coverage,
)
from healthmes.store.enums import CalendarSource


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda _tmp_path: InMemorySyncHealthStore(),
        lambda tmp_path: FileSyncHealthStore(tmp_path),
    ],
)
class TestSyncHealthStoreContract:
    def test_never_synced_and_empty_success_are_distinct(
        self,
        store_factory,
        tmp_path,
    ) -> None:
        store = store_factory(tmp_path)
        assert store.load(CalendarSource.GOOGLE) is None

        attempted_at = utc(2026, 8, 12, 9)
        succeeded_at = attempted_at + timedelta(seconds=2)
        store.record_attempt(CalendarSource.GOOGLE, attempted_at)
        attempted = store.load(CalendarSource.GOOGLE)
        assert attempted is not None
        assert attempted.status is SyncHealthStatus.NEVER_SYNCED

        store.record_success(
            CalendarSource.GOOGLE,
            succeeded_at,
            event_count=0,
        )
        successful = store.load(CalendarSource.GOOGLE)
        assert successful is not None
        assert successful.status is SyncHealthStatus.EMPTY_SUCCESS
        assert successful.last_attempt_at == attempted_at
        assert successful.last_success_at == succeeded_at
        assert successful.last_success_event_count == 0

    def test_success_coverage_is_query_bounded(
        self,
        store_factory,
        tmp_path,
    ) -> None:
        store = store_factory(tmp_path)
        start = utc(2026, 8, 1)
        end = utc(2026, 9, 1)
        store.record_success(
            CalendarSource.GOOGLE,
            utc(2026, 8, 12, 9),
            event_count=0,
            coverage_kind=SyncCoverageKind.BOUNDED_WINDOW,
            coverage_start=start,
            coverage_end=end,
        )

        state = store.load(CalendarSource.GOOGLE)
        assert state is not None
        assert state.covers(utc(2026, 8, 10), utc(2026, 8, 11))
        assert not state.covers(utc(2026, 7, 31), utc(2026, 8, 1))

    def test_success_then_failure_preserves_success_history(
        self,
        store_factory,
        tmp_path,
    ) -> None:
        store = store_factory(tmp_path)
        success_at = utc(2026, 8, 12, 9)
        failure_at = success_at + timedelta(minutes=5)
        store.record_success(
            CalendarSource.GOOGLE,
            success_at,
            event_count=4,
        )

        store.record_failure(
            CalendarSource.GOOGLE,
            failure_at,
            error_code="calendar_auth_error",
        )

        state = store.load(CalendarSource.GOOGLE)
        assert state is not None
        assert state.status is SyncHealthStatus.RECENT_FAILURE
        assert state.last_success_at == success_at
        assert state.last_success_event_count == 4
        assert state.last_failure_at == failure_at
        assert state.last_error_code == "calendar_auth_error"

    def test_failure_then_success_preserves_failure_history(
        self,
        store_factory,
        tmp_path,
    ) -> None:
        store = store_factory(tmp_path)
        failure_at = utc(2026, 8, 12, 9)
        success_at = failure_at + timedelta(minutes=5)
        store.record_failure(
            CalendarSource.CALDAV,
            failure_at,
            error_code="calendar_timeout",
        )

        store.record_success(
            CalendarSource.CALDAV,
            success_at,
            event_count=2,
        )

        state = store.load(CalendarSource.CALDAV)
        assert state is not None
        assert state.status is SyncHealthStatus.SUCCESS
        assert state.last_failure_at == failure_at
        assert state.last_error_code == "calendar_timeout"
        assert state.last_success_at == success_at
        assert state.last_success_event_count == 2

    def test_writeback_failure_does_not_replace_inbound_success(
        self,
        store_factory,
        tmp_path,
    ) -> None:
        store = store_factory(tmp_path)
        inbound_at = utc(2026, 8, 12, 9)
        writeback_attempted_at = inbound_at + timedelta(seconds=1)
        writeback_failed_at = inbound_at + timedelta(seconds=2)
        store.record_success(
            CalendarSource.GOOGLE,
            inbound_at,
            event_count=0,
            coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        )
        store.record_writeback_attempt(
            CalendarSource.GOOGLE,
            writeback_attempted_at,
            attempted_count=2,
        )
        store.record_writeback_result(
            CalendarSource.GOOGLE,
            writeback_failed_at,
            attempted_count=2,
            succeeded_count=1,
            failed_count=1,
        )

        state = store.load(CalendarSource.GOOGLE)
        assert state is not None
        assert state.status is SyncHealthStatus.EMPTY_SUCCESS
        assert state.last_success_at == inbound_at
        assert state.writeback_last_attempt_at == writeback_attempted_at
        assert state.writeback_last_failure_at == writeback_failed_at
        assert (
            state.writeback_last_error_code
            == "calendar_writeback_partial_failure"
        )
        assert state.writeback_attempted_count == 2
        assert state.writeback_succeeded_count == 1
        assert state.writeback_failed_count == 1


class TestFileSyncHealthStore:
    def test_per_source_files_are_isolated_and_atomic(self, tmp_path) -> None:
        store = FileSyncHealthStore(tmp_path)
        store.record_success(
            CalendarSource.GOOGLE,
            utc(2026, 8, 12, 9),
            event_count=3,
        )
        google_path = store.path_for(CalendarSource.GOOGLE)
        google_bytes = google_path.read_bytes()

        store.record_failure(
            CalendarSource.CALDAV,
            utc(2026, 8, 12, 9, 5),
            error_code="calendar_auth_error",
        )

        assert google_path.read_bytes() == google_bytes
        assert google_path != store.path_for(CalendarSource.CALDAV)
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "sync_health.caldav.json",
            "sync_health.google.json",
        ]

    def test_corrupt_file_fails_safe_and_recovers(self, tmp_path) -> None:
        store = FileSyncHealthStore(tmp_path)
        path = store.path_for(CalendarSource.GOOGLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"source":"google","latest_outcome":"success","credential":"secret"',
            encoding="utf-8",
        )

        assert store.load(CalendarSource.GOOGLE) is None

        store.record_success(
            CalendarSource.GOOGLE,
            utc(2026, 8, 12, 9),
            event_count=0,
        )
        recovered = store.load(CalendarSource.GOOGLE)
        assert recovered is not None
        assert recovered.status is SyncHealthStatus.EMPTY_SUCCESS
        assert "credential" not in json.loads(path.read_text(encoding="utf-8"))

    def test_invalid_source_file_fails_safe(self, tmp_path) -> None:
        store = FileSyncHealthStore(tmp_path)
        path = store.path_for(CalendarSource.GOOGLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "caldav",
                    "latest_outcome": None,
                }
            ),
            encoding="utf-8",
        )

        assert store.load(CalendarSource.GOOGLE) is None

    def test_atomic_write_failure_removes_unique_temp(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        store = FileSyncHealthStore(tmp_path)

        def fail_replace(_source, _target) -> None:
            raise OSError("simulated atomic replace failure")

        monkeypatch.setattr("healthmes.calendars.state.os.replace", fail_replace)
        with pytest.raises(OSError, match="atomic replace"):
            store.record_attempt(CalendarSource.GOOGLE, utc(2026, 8, 12, 9))

        assert list(tmp_path.iterdir()) == []

    def test_payload_contains_only_operational_fields(self, tmp_path) -> None:
        store = FileSyncHealthStore(tmp_path)
        store.record_failure(
            CalendarSource.GOOGLE,
            utc(2026, 8, 12, 9),
            error_code="calendar_auth_error",
        )

        payload = json.loads(
            store.path_for(CalendarSource.GOOGLE).read_text(encoding="utf-8")
        )
        assert set(payload) == {
            "coverage_end",
            "coverage_kind",
            "coverage_start",
            "version",
            "source",
            "last_attempt_at",
            "last_success_at",
            "last_failure_at",
            "last_error_code",
            "last_success_event_count",
            "latest_outcome",
            "writeback_attempted_count",
            "writeback_failed_count",
            "writeback_last_attempt_at",
            "writeback_last_error_code",
            "writeback_last_failure_at",
            "writeback_last_success_at",
            "writeback_succeeded_count",
        }
        encoded = json.dumps(payload)
        assert "event title" not in encoded
        assert "credential-secret" not in encoded
        assert "provider exception message" not in encoded

    def test_satisfies_protocol(self, tmp_path) -> None:
        assert isinstance(FileSyncHealthStore(tmp_path), SyncHealthStore)
        assert isinstance(InMemorySyncHealthStore(), SyncHealthStore)

    def test_v1_payload_loads_with_unknown_coverage(self, tmp_path) -> None:
        store = FileSyncHealthStore(tmp_path)
        path = store.path_for(CalendarSource.GOOGLE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "google",
                    "last_attempt_at": None,
                    "last_success_at": "2026-08-12T09:00:00+00:00",
                    "last_failure_at": None,
                    "last_error_code": None,
                    "last_success_event_count": 0,
                    "latest_outcome": "success",
                }
            ),
            encoding="utf-8",
        )

        state = store.load(CalendarSource.GOOGLE)
        assert state is not None
        assert state.coverage_kind is SyncCoverageKind.UNKNOWN
        assert not state.covers(utc(2026, 8, 10), utc(2026, 8, 11))


def test_sync_state_coverage_round_trip_and_invalid_legacy_fallback() -> None:
    start = utc(2026, 8, 1)
    end = utc(2026, 9, 1)
    cursor = with_sync_state_coverage(
        {"sync_token": "token"},
        kind=SyncCoverageKind.BOUNDED_WINDOW,
        start=start,
        end=end,
    )

    assert sync_state_coverage(cursor) == (
        SyncCoverageKind.BOUNDED_WINDOW,
        start,
        end,
    )
    assert sync_state_coverage({"sync_token": "legacy"}) == (
        SyncCoverageKind.UNKNOWN,
        None,
        None,
    )


class TestInMemorySyncStateStore:
    def test_round_trip_and_isolation(self) -> None:
        store = InMemorySyncStateStore()
        assert store.load(CalendarSource.GOOGLE) is None

        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        store.save(CalendarSource.CALDAV, {"ctag": "c1"})
        assert store.load(CalendarSource.GOOGLE) == {"sync_token": "t1"}
        assert store.load(CalendarSource.CALDAV) == {"ctag": "c1"}

    def test_returns_copies(self) -> None:
        store = InMemorySyncStateStore()
        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        loaded = store.load(CalendarSource.GOOGLE)
        assert loaded is not None
        loaded["sync_token"] = "mutated"
        assert store.load(CalendarSource.GOOGLE) == {"sync_token": "t1"}

    def test_clear(self) -> None:
        store = InMemorySyncStateStore()
        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        store.clear(CalendarSource.GOOGLE)
        assert store.load(CalendarSource.GOOGLE) is None


class TestFileSyncStateStore:
    def test_missing_dir_means_never_synced(self, tmp_path) -> None:
        store = FileSyncStateStore(tmp_path / "calendars")
        assert store.load(CalendarSource.GOOGLE) is None

    def test_round_trip_persists_across_instances(self, tmp_path) -> None:
        directory = tmp_path / "nested" / "calendars"
        FileSyncStateStore(directory).save(
            CalendarSource.GOOGLE, {"sync_token": "t1", "known_ids": {"a": "e1"}}
        )
        reopened = FileSyncStateStore(directory)
        assert reopened.load(CalendarSource.GOOGLE) == {
            "sync_token": "t1",
            "known_ids": {"a": "e1"},
        }
        assert reopened.load(CalendarSource.CALDAV) is None

    def test_save_replaces_only_that_source(self, tmp_path) -> None:
        store = FileSyncStateStore(tmp_path)
        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        store.save(CalendarSource.CALDAV, {"ctag": "c1"})
        store.save(CalendarSource.GOOGLE, {"sync_token": "t2"})
        assert store.load(CalendarSource.GOOGLE) == {"sync_token": "t2"}
        assert store.load(CalendarSource.CALDAV) == {"ctag": "c1"}

    def test_corrupted_file_degrades_to_full_resync(self, tmp_path) -> None:
        store = FileSyncStateStore(tmp_path)
        target = store.path_for(CalendarSource.GOOGLE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json", encoding="utf-8")
        assert store.load(CalendarSource.GOOGLE) is None
        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})  # recovers by rewriting
        assert store.load(CalendarSource.GOOGLE) == {"sync_token": "t1"}

    def test_no_temp_file_left_behind(self, tmp_path) -> None:
        FileSyncStateStore(tmp_path).save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        assert [p.name for p in tmp_path.iterdir()] == ["sync_state.google.json"]

    def test_concurrent_sources_do_not_clobber_each_other(self, tmp_path) -> None:
        # F7: per-source files mean writing one source never rewrites (and so
        # can never lose) another source's state — the failure mode of a single
        # shared read-modify-write document under overlapping polls.
        store = FileSyncStateStore(tmp_path)
        store.save(CalendarSource.GOOGLE, {"sync_token": "g1"})
        google_file = store.path_for(CalendarSource.GOOGLE)
        google_bytes = google_file.read_bytes()

        store.save(CalendarSource.CALDAV, {"ctag": "c1"})

        assert google_file.read_bytes() == google_bytes  # untouched by caldav write
        assert store.load(CalendarSource.GOOGLE) == {"sync_token": "g1"}
        assert store.load(CalendarSource.CALDAV) == {"ctag": "c1"}
        assert google_file != store.path_for(CalendarSource.CALDAV)

    def test_clear_one_source_leaves_others(self, tmp_path) -> None:
        store = FileSyncStateStore(tmp_path)
        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        store.save(CalendarSource.CALDAV, {"ctag": "c1"})
        store.clear(CalendarSource.GOOGLE)
        assert store.load(CalendarSource.GOOGLE) is None
        assert store.load(CalendarSource.CALDAV) == {"ctag": "c1"}

    def test_for_data_dir_layout(self, tmp_path) -> None:
        store = FileSyncStateStore.for_data_dir(tmp_path)
        assert store.directory == tmp_path / "calendars"
        assert store.path_for(CalendarSource.GOOGLE) == (
            tmp_path / "calendars" / "sync_state.google.json"
        )

    def test_satisfies_protocol(self, tmp_path) -> None:
        assert isinstance(FileSyncStateStore(tmp_path), SyncStateStore)
        assert isinstance(InMemorySyncStateStore(), SyncStateStore)


class TestPendingDiffStore:
    def test_file_round_trip_and_clear(self, tmp_path) -> None:
        store = FilePendingDiffStore(tmp_path)
        assert store.load(CalendarSource.GOOGLE) is None
        payload = {
            "created": [],
            "moved": [],
            "deleted": [{"external_id": "gone-1", "kind": "deleted"}],
            "agent_modified": [],
        }
        store.save(CalendarSource.GOOGLE, payload)
        assert store.load(CalendarSource.GOOGLE) == payload
        store.clear(CalendarSource.GOOGLE)
        assert store.load(CalendarSource.GOOGLE) is None

    def test_file_per_source_isolation(self, tmp_path) -> None:
        store = FilePendingDiffStore(tmp_path)
        store.save(CalendarSource.GOOGLE, {"deleted": ["g"]})
        store.save(CalendarSource.CALDAV, {"deleted": ["c"]})
        assert store.load(CalendarSource.GOOGLE) == {"deleted": ["g"]}
        assert store.load(CalendarSource.CALDAV) == {"deleted": ["c"]}
        assert store.path_for(CalendarSource.GOOGLE) != store.path_for(CalendarSource.CALDAV)

    def test_in_memory_round_trip_is_copied(self, tmp_path) -> None:
        store = InMemoryPendingDiffStore()
        store.save(CalendarSource.GOOGLE, {"deleted": [{"external_id": "x"}]})
        loaded = store.load(CalendarSource.GOOGLE)
        assert loaded == {"deleted": [{"external_id": "x"}]}
        loaded["deleted"].append("mutation")
        assert store.load(CalendarSource.GOOGLE) == {"deleted": [{"external_id": "x"}]}

    def test_satisfies_protocol(self, tmp_path) -> None:
        assert isinstance(FilePendingDiffStore(tmp_path), PendingDiffStore)
        assert isinstance(InMemoryPendingDiffStore(), PendingDiffStore)
