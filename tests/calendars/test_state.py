"""Sync-state / journal store tests: round-trips, per-source isolation, corruption."""

from healthmes.calendars.state import (
    FilePendingDiffStore,
    FileSyncStateStore,
    InMemoryPendingDiffStore,
    InMemorySyncStateStore,
    PendingDiffStore,
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
