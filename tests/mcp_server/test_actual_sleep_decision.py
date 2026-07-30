import datetime as dt

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    ScheduleProposal,
    Task,
)

LOCAL_DATE = dt.date(2026, 7, 8)


def _seed_actual_sleep(store_factory, pinned_tz) -> None:
    start = dt.datetime(2026, 7, 7, 23, 30, tzinfo=pinned_tz)
    wake = dt.datetime(2026, 7, 8, 7, 0, tzinfo=pinned_tz)
    with store_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id="actual-sleep-2026-07-08",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=start.astimezone(dt.UTC),
                end_at=wake.astimezone(dt.UTC),
                is_agent_created=True,
                healthmes_kind="actual_sleep",
                healthmes_source="oura",
                healthmes_source_key="oura:2026-07-08",
                observation_fingerprint="fingerprint",
                sleep_local_date=LOCAL_DATE,
                sleep_duration_minutes=420,
                sleep_time_in_bed_minutes=450,
            )
        )
        session.commit()


async def test_readiness_exposes_actual_sleep_without_changing_confidence(
    mcp_client,
    mcp_env,
    call_tool,
    store_factory,
    pinned_tz,
) -> None:
    # Given
    _seed_actual_sleep(store_factory, pinned_tz)

    # When
    result = await call_tool(
        mcp_client,
        "get_daily_readiness_context",
        {"date": LOCAL_DATE.isoformat()},
    )

    # Then
    assert result["confidence"] == "low"
    assert result["actual_sleep"] == {
        "status": "ok",
        "local_date": "2026-07-08",
        "start": "2026-07-07T23:30:00+09:00",
        "wake_time": "2026-07-08T07:00:00+09:00",
        "duration_minutes": 420,
        "time_in_bed_minutes": 450,
        "source": "oura",
        "freshness": "current",
        "earliest_available_work_time": "2026-07-08T07:00:00+09:00",
    }


async def test_readiness_uses_fresh_oura_sleep_before_calendar_mirror_catches_up(
    mcp_client,
    mcp_env,
    call_tool,
) -> None:
    mcp_env.add_sleep_summary(
        LOCAL_DATE.isoformat(),
        start_time="2026-07-07T23:30:00+09:00",
        end_time="2026-07-08T07:00:00+09:00",
        duration_minutes=420,
        time_in_bed_minutes=450,
    )

    result = await call_tool(
        mcp_client,
        "get_daily_readiness_context",
        {"date": LOCAL_DATE.isoformat()},
    )

    assert result["actual_sleep"] == {
        "status": "ok",
        "local_date": "2026-07-08",
        "start": "2026-07-07T23:30:00+09:00",
        "wake_time": "2026-07-08T07:00:00+09:00",
        "duration_minutes": 420,
        "time_in_bed_minutes": 450,
        "source": "oura",
        "freshness": "current",
        "earliest_available_work_time": "2026-07-08T07:00:00+09:00",
    }


async def test_proposal_before_actual_wake_is_rejected_without_side_effects(
    mcp_client,
    store_factory,
    pinned_tz,
) -> None:
    # Given
    _seed_actual_sleep(store_factory, pinned_tz)
    start = dt.datetime(2026, 7, 8, 6, 30, tzinfo=pinned_tz)

    # When / Then
    with pytest.raises(ToolError, match="actual wake time"):
        await mcp_client.call_tool(
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "title": "Too early",
                        "start": start.isoformat(),
                        "end": (start + dt.timedelta(hours=1)).isoformat(),
                    }
                ]
            },
        )
    with store_factory() as session:
        assert list(session.scalars(select(Task))) == []
        assert list(session.scalars(select(ScheduleProposal))) == []


async def test_proposal_after_actual_wake_is_allowed(
    mcp_client,
    call_tool,
    store_factory,
    pinned_tz,
) -> None:
    # Given
    _seed_actual_sleep(store_factory, pinned_tz)
    start = dt.datetime(2026, 7, 8, 7, 30, tzinfo=pinned_tz)

    # When
    result = await call_tool(
        mcp_client,
        "propose_schedule_blocks",
        {
            "blocks": [
                {
                    "title": "After wake",
                    "start": start.isoformat(),
                    "end": (start + dt.timedelta(hours=1)).isoformat(),
                }
            ]
        },
    )

    # Then
    assert result["proposals"][0]["task_title"] == "After wake"


async def test_missing_actual_sleep_preserves_existing_proposal_behavior(
    mcp_client,
    call_tool,
    pinned_tz,
) -> None:
    # Given
    start = dt.datetime(2026, 7, 8, 6, 30, tzinfo=pinned_tz)

    # When
    result = await call_tool(
        mcp_client,
        "propose_schedule_blocks",
        {
            "blocks": [
                {
                    "title": "No observation",
                    "start": start.isoformat(),
                    "end": (start + dt.timedelta(hours=1)).isoformat(),
                }
            ]
        },
    )

    # Then
    assert result["proposals"][0]["task_title"] == "No observation"
