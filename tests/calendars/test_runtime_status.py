import healthmes.calendars.runtime_status as runtime_status
from healthmes.calendars.runtime_status import read_calendar_status, record_calendar_status
from healthmes.store.enums import CalendarSource


def test_record_calendar_status_preserves_each_provider_entry(tmp_path) -> None:
    record_calendar_status(tmp_path, CalendarSource.GOOGLE, mode="mirror")
    record_calendar_status(tmp_path, CalendarSource.CALDAV, mode="write")

    status = read_calendar_status(tmp_path)

    assert status[CalendarSource.GOOGLE.value]["mode"] == "mirror"
    assert status[CalendarSource.CALDAV.value]["mode"] == "write"


def test_record_calendar_status_keeps_sync_failure_contained(tmp_path, monkeypatch) -> None:
    def unavailable(_path):
        raise OSError("unavailable")

    monkeypatch.setattr(runtime_status, "_status_lock", unavailable)

    record_calendar_status(tmp_path, CalendarSource.GOOGLE, mode="mirror")


def test_read_calendar_status_ignores_non_utf8_contents(tmp_path) -> None:
    path = tmp_path / "runtime" / "calendar-status.json"
    path.parent.mkdir()
    path.write_bytes(b"\xff")

    assert read_calendar_status(tmp_path) == {}
