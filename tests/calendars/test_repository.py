from datetime import UTC, datetime

from sqlalchemy import select

from healthmes.calendars.repository import retained_calendar_statement
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    RetentionPolicy,
)


def test_retained_calendar_statement_hides_rows_before_maintenance(
    session,
) -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    session.add(
        RetentionPolicy(
            data_class="calendar_mirror",
            retention_days=1,
            enabled=True,
        )
    )
    session.add_all(
        [
            CalendarEventMirror(
                external_id="expired",
                calendar_source=CalendarSource.GOOGLE,
                summary="Expired private event",
                start_at=datetime(2026, 8, 12, 9, tzinfo=UTC),
                end_at=datetime(2026, 8, 12, 10, tzinfo=UTC),
                is_agent_created=False,
            ),
            CalendarEventMirror(
                external_id="retained",
                calendar_source=CalendarSource.GOOGLE,
                summary="Retained event",
                start_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
                end_at=datetime(2026, 8, 14, 10, tzinfo=UTC),
                is_agent_created=False,
            ),
        ]
    )
    session.flush()

    statement = retained_calendar_statement(
        session,
        select(CalendarEventMirror).order_by(
            CalendarEventMirror.external_id
        ),
        now=now,
    )

    assert [
        row.external_id for row in session.scalars(statement)
    ] == ["retained"]
