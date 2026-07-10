"""Contract tests: ExternalEvent/EventDraft normalization and the protocol."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from healthmes.calendars.base import (
    CalendarBackend,
    EventDraft,
    ExternalEvent,
    coerce_utc,
    ensure_utc,
    parse_task_id,
)

KST = timezone(timedelta(hours=9))


class TestEnsureUtc:
    def test_converts_aware_to_utc(self) -> None:
        value = datetime(2026, 7, 9, 18, 0, tzinfo=KST)
        assert ensure_utc(value) == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)

    def test_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            ensure_utc(datetime(2026, 7, 9, 18, 0))


class TestCoerceUtc:
    def test_naive_assumed_utc(self) -> None:
        assert coerce_utc(datetime(2026, 7, 9, 9, 0)) == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)

    def test_aware_converted(self) -> None:
        value = datetime(2026, 7, 9, 18, 0, tzinfo=KST)
        assert coerce_utc(value) == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)


class TestParseTaskId:
    def test_valid_uuid_string(self) -> None:
        task_id = uuid.uuid4()
        assert parse_task_id(str(task_id)) == task_id

    def test_passthrough_uuid(self) -> None:
        task_id = uuid.uuid4()
        assert parse_task_id(task_id) == task_id

    @pytest.mark.parametrize("value", [None, "", "not-a-uuid", 42])
    def test_invalid_values_return_none(self, value: object) -> None:
        assert parse_task_id(value) is None


class TestExternalEvent:
    def test_normalizes_times_to_utc(self) -> None:
        event = ExternalEvent(
            external_id="e1",
            start_at=datetime(2026, 7, 9, 18, 0, tzinfo=KST),
            end_at=datetime(2026, 7, 9, 19, 0, tzinfo=KST),
        )
        assert event.start_at == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)
        assert event.end_at == datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
        assert event.start_at.tzinfo == UTC

    def test_live_event_requires_times(self) -> None:
        with pytest.raises(ValueError, match="requires start_at and end_at"):
            ExternalEvent(external_id="e1")

    def test_deleted_event_needs_only_id(self) -> None:
        event = ExternalEvent(external_id="gone", deleted=True)
        assert event.start_at is None and event.end_at is None

    def test_rejects_naive_times(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            ExternalEvent(
                external_id="e1",
                start_at=datetime(2026, 7, 9, 9, 0),
                end_at=datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
            )

    def test_rejects_end_before_start(self) -> None:
        with pytest.raises(ValueError, match="end_at"):
            ExternalEvent(
                external_id="e1",
                start_at=datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
            )

    def test_rejects_empty_id(self) -> None:
        with pytest.raises(ValueError, match="external_id"):
            ExternalEvent(external_id="", deleted=True)


class TestEventDraft:
    def test_normalizes_and_validates(self) -> None:
        draft = EventDraft(
            summary="Deep work",
            start_at=datetime(2026, 7, 9, 18, 0, tzinfo=KST),
            end_at=datetime(2026, 7, 9, 20, 0, tzinfo=KST),
        )
        assert draft.start_at == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)

    def test_rejects_zero_length(self) -> None:
        instant = datetime(2026, 7, 9, 9, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="after start_at"):
            EventDraft(summary="x", start_at=instant, end_at=instant)

    def test_rejects_empty_summary(self) -> None:
        with pytest.raises(ValueError, match="summary"):
            EventDraft(
                summary="",
                start_at=datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
            )


class TestProtocolConformance:
    def test_fake_backend_satisfies_protocol(self, fake_backend) -> None:
        assert isinstance(fake_backend, CalendarBackend)

    def test_real_backends_satisfy_protocol(self) -> None:
        from healthmes.calendars.caldav_icloud import CalDavCalendarBackend
        from healthmes.calendars.google import GoogleCalendarBackend

        assert isinstance(GoogleCalendarBackend(service=object()), CalendarBackend)
        assert isinstance(CalDavCalendarBackend(calendar=object()), CalendarBackend)
