import datetime as dt
import json

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthmes.mcp_server import server as server_module
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    ScheduleProposal,
    Task,
)

LOCAL_DATE = dt.date(2026, 7, 8)
GOOGLE_ACCOUNT_GENERATION = "b" * 32


def _seed_actual_sleep(store_factory, pinned_tz) -> None:
    token_path = (
        server_module._active_settings().data_dir
        / "google"
        / "calendar_token.json"
    )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": "fake-refresh",
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "fake-secret",
                "_healthmes_account_generation": (
                    GOOGLE_ACCOUNT_GENERATION
                ),
            }
        ),
        encoding="utf-8",
    )
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
                connection_generation=GOOGLE_ACCOUNT_GENERATION,
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
    actual_sleep_refs = [
        source_ref
        for source_ref in result["source_refs"]
        if source_ref["resource_type"] == "actual_sleep"
    ]
    assert len(actual_sleep_refs) == 1
    assert actual_sleep_refs[0] == {
        "domain": "wearable",
        "record_id": actual_sleep_refs[0]["record_id"],
        "source_provider": "healthmes-calendar-mirror",
        "upstream_provider": "oura",
        "resource_type": "actual_sleep",
        "observed_at": "2026-07-07T22:00:00+00:00",
        "calendar_source": "google",
        "account_generation": GOOGLE_ACCOUNT_GENERATION,
        "schema_version": 1,
        "derived_by": "healthmes.actual-sleep-mirror.v1",
    }
    assert actual_sleep_refs[0]["record_id"] in result["evidence_ids"]


async def test_readiness_uses_fresh_oura_sleep_before_calendar_mirror_catches_up(
    mcp_client,
    mcp_env,
    call_tool,
) -> None:
    mcp_env.add_sleep_summary(
        (LOCAL_DATE - dt.timedelta(days=1)).isoformat(),
        duration_minutes="malformed historical value",
    )
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


async def test_readiness_references_only_selected_fresh_sleep_summary(
    mcp_client,
    mcp_env,
    call_tool,
) -> None:
    mcp_env.add_sleep_summary(
        LOCAL_DATE.isoformat(),
        id="short-sleep",
        start_time="2026-07-08T01:00:00+09:00",
        end_time="2026-07-08T05:00:00+09:00",
        duration_minutes=240,
        time_in_bed_minutes=240,
    )
    mcp_env.add_sleep_summary(
        LOCAL_DATE.isoformat(),
        id="selected-long-sleep",
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
    sleep_refs = [
        source_ref
        for source_ref in result["source_refs"]
        if source_ref["resource_type"] == "sleep_summary"
    ]

    assert [source_ref["record_id"] for source_ref in sleep_refs] == [
        "selected-long-sleep"
    ]
    assert "short-sleep" not in result["evidence_ids"]


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


async def test_cross_midnight_proposal_cannot_bypass_actual_sleep_overlap(
    mcp_client,
    store_factory,
    pinned_tz,
) -> None:
    _seed_actual_sleep(store_factory, pinned_tz)
    start = dt.datetime(2026, 7, 7, 23, 45, tzinfo=pinned_tz)

    with pytest.raises(ToolError, match="overlaps actual sleep"):
        await mcp_client.call_tool(
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "title": "Cross-midnight overlap",
                        "start": start.isoformat(),
                        "end": (start + dt.timedelta(minutes=45)).isoformat(),
                    }
                ]
            },
        )

    with store_factory() as session:
        assert list(session.scalars(select(Task))) == []
        assert list(session.scalars(select(ScheduleProposal))) == []


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
