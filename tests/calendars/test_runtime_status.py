from healthmes.calendars.runtime_status import read_calendar_status, record_calendar_status
from healthmes.store.enums import CalendarSource


def test_record_calendar_status_preserves_each_provider_entry(tmp_path) -> None:
    record_calendar_status(tmp_path, CalendarSource.GOOGLE, mode="mirror")
    record_calendar_status(tmp_path, CalendarSource.CALDAV, mode="write")

    status = read_calendar_status(tmp_path)

    assert status[CalendarSource.GOOGLE.value]["mode"] == "mirror"
    assert status[CalendarSource.CALDAV.value]["mode"] == "write"
