"""Sync-state store tests: round-trips, isolation per source, corruption safety."""

from healthmes.calendars.state import (
    FileSyncStateStore,
    InMemorySyncStateStore,
    SyncStateStore,
)
from healthmes.store.enums import CalendarSource


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
    def test_missing_file_means_never_synced(self, tmp_path) -> None:
        store = FileSyncStateStore(tmp_path / "sync_state.json")
        assert store.load(CalendarSource.GOOGLE) is None

    def test_round_trip_persists_across_instances(self, tmp_path) -> None:
        path = tmp_path / "nested" / "sync_state.json"
        FileSyncStateStore(path).save(
            CalendarSource.GOOGLE, {"sync_token": "t1", "known_ids": {"a": "e1"}}
        )
        reopened = FileSyncStateStore(path)
        assert reopened.load(CalendarSource.GOOGLE) == {
            "sync_token": "t1",
            "known_ids": {"a": "e1"},
        }
        assert reopened.load(CalendarSource.CALDAV) is None

    def test_save_replaces_only_that_source(self, tmp_path) -> None:
        path = tmp_path / "sync_state.json"
        store = FileSyncStateStore(path)
        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        store.save(CalendarSource.CALDAV, {"ctag": "c1"})
        store.save(CalendarSource.GOOGLE, {"sync_token": "t2"})
        assert store.load(CalendarSource.GOOGLE) == {"sync_token": "t2"}
        assert store.load(CalendarSource.CALDAV) == {"ctag": "c1"}

    def test_corrupted_file_degrades_to_full_resync(self, tmp_path) -> None:
        path = tmp_path / "sync_state.json"
        path.write_text("{not json", encoding="utf-8")
        store = FileSyncStateStore(path)
        assert store.load(CalendarSource.GOOGLE) is None
        store.save(CalendarSource.GOOGLE, {"sync_token": "t1"})  # recovers by rewriting
        assert store.load(CalendarSource.GOOGLE) == {"sync_token": "t1"}

    def test_no_temp_file_left_behind(self, tmp_path) -> None:
        path = tmp_path / "sync_state.json"
        FileSyncStateStore(path).save(CalendarSource.GOOGLE, {"sync_token": "t1"})
        assert [p.name for p in tmp_path.iterdir()] == ["sync_state.json"]

    def test_for_data_dir_layout(self, tmp_path) -> None:
        store = FileSyncStateStore.for_data_dir(tmp_path)
        assert store.path == tmp_path / "calendars" / "sync_state.json"

    def test_satisfies_protocol(self, tmp_path) -> None:
        assert isinstance(FileSyncStateStore(tmp_path / "s.json"), SyncStateStore)
        assert isinstance(InMemorySyncStateStore(), SyncStateStore)
