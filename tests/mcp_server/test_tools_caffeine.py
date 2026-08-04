import datetime as dt
import uuid

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthmes.mcp_server import server as server_module
from healthmes.store import CalendarEventMirror, CalendarSource


def _seed_event(
    store_factory,
    *,
    start: dt.datetime,
    end: dt.datetime,
    summary: str = "Focused work",
) -> uuid.UUID:
    with store_factory() as session:
        event = CalendarEventMirror(
            external_id=f"caffeine-{uuid.uuid4()}",
            calendar_source=CalendarSource.GOOGLE,
            summary=summary,
            start_at=start,
            end_at=end,
        )
        session.add(event)
        session.flush()
        event_id = event.id
        session.commit()
    return event_id


def _proposal_args(
    event_id: uuid.UUID,
    *,
    event_start_local: dt.datetime,
    target_sleep_local: dt.datetime,
) -> dict[str, object]:
    return {
        "event_id": str(event_id),
        "personal_daily_limit_mg": 300,
        "population_status": "confirmed_adult",
        "product_form": "beverage_or_food",
        "intended_consumption_at": event_start_local.isoformat(),
        "target_sleep_at": target_sleep_local.isoformat(),
        "consumed_today_mg": 100,
        "total_intake_complete": True,
        "personal_event_baseline_mg": 100,
        "baseline_confirmed_at": (event_start_local - dt.timedelta(hours=1)).isoformat(),
        "cutoff_before_sleep_hours": 6,
        "contraindications": [],
    }


def _local_times(
    pinned_tz: dt.tzinfo,
    *,
    days_from_today: int = 0,
) -> tuple[dt.date, dt.datetime, dt.datetime]:
    day = dt.datetime.now(pinned_tz).date() + dt.timedelta(days=days_from_today)
    event_start = dt.datetime.combine(day, dt.time(13), tzinfo=pinned_tz)
    target_sleep = dt.datetime.combine(day, dt.time(23), tzinfo=pinned_tz)
    return day, event_start, target_sleep


class TestCaffeineProposalTool:
    async def test_current_evidence_returns_personal_baseline_proposal_without_writes(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _proposal_args(
                event_id,
                event_start_local=event_start,
                target_sleep_local=target_sleep,
            ),
        )

        assert result["status"] == "proposal"
        assert result["facts"]["target_event"]["id"] == str(event_id)
        assert result["facts"]["sleep"] == {
            "local_date": day.isoformat(),
            "duration_minutes": 374,
            "provider": "oura",
            "source_key": f"sleep-summary:oura:{day.isoformat()}",
            "freshness": "current",
        }
        assert result["facts"]["personal_daily_limit"] == {
            "amount_mg": 300,
            "source": "user_confirmed_via_agent",
        }
        assert result["facts"]["remaining_daily_allowance_mg"] == 200
        assert result["recommendation"] == {
            "maximum_additional_mg": 200,
            "suggested_additional_mg": 100,
            "basis": "personal_event_baseline",
        }
        assert result["confidence"] == "medium"
        assert result["reason"] == "personal_event_baseline_applied"
        assert result["framing"] == "bounded_preparation_proposal_not_medical_advice"

        with store_factory() as session:
            events = list(session.scalars(select(CalendarEventMirror)))
        assert [(event.id, event.summary) for event in events] == [(event_id, "Focused work")]

    async def test_unknown_event_fails_closed_without_provider_lookup(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        pinned_tz,
    ):
        _, event_start, target_sleep = _local_times(pinned_tz)
        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _proposal_args(
                uuid.uuid4(),
                event_start_local=event_start,
                target_sleep_local=target_sleep,
            ),
        )

        assert result["status"] == "insufficient_data"
        assert result["reason"] == "missing_target_event"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert mcp_env.requests == []

    async def test_future_event_sleep_is_stale_for_today_proposal(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz, days_from_today=1)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _proposal_args(
                event_id,
                event_start_local=event_start,
                target_sleep_local=target_sleep,
            ),
        )

        assert result["status"] == "insufficient_data"
        assert result["facts"]["sleep"]["freshness"] == "stale"
        assert result["reason"] == "stale_sleep"
        assert result["recommendation"]["maximum_additional_mg"] is None

    async def test_missing_sleep_and_incomplete_intake_do_not_invent_numbers(
        self,
        mcp_client,
        call_tool,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["consumed_today_mg"] = None
        args["total_intake_complete"] = False

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

        assert result["status"] == "insufficient_data"
        assert result["facts"]["sleep"] is None
        assert result["facts"]["sleep_adapter_reason"] == "no_complete_sleep_summary"
        assert result["recommendation"] == {
            "maximum_additional_mg": None,
            "suggested_additional_mg": None,
            "basis": "unavailable",
        }
        assert result["reason"] == "missing_sleep"
        assert result["facts"]["target_event"]["start"].startswith(day.isoformat())

    async def test_unconfirmed_baseline_returns_only_an_upper_bound(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["baseline_confirmed_at"] = None

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

        assert result["status"] == "proposal"
        assert result["recommendation"]["maximum_additional_mg"] == 200
        assert result["recommendation"]["suggested_additional_mg"] is None
        assert result["recommendation"]["basis"] == "upper_bound_only"
        assert result["reason"] == "personal_event_baseline_unavailable"

    async def test_stale_baseline_returns_only_an_upper_bound(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["baseline_confirmed_at"] = (event_start - dt.timedelta(days=2)).isoformat()

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

        assert result["status"] == "proposal"
        assert result["facts"]["personal_event_baseline"]["freshness"] == "stale"
        assert result["recommendation"] == {
            "maximum_additional_mg": 200,
            "suggested_additional_mg": None,
            "basis": "upper_bound_only",
        }
        assert result["reason"] == "personal_event_baseline_unavailable"

    async def test_future_baseline_confirmation_is_not_current_evidence(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
        monkeypatch,
    ):
        now = dt.datetime.now(pinned_tz)
        event_start = now + dt.timedelta(hours=2)
        target_sleep = event_start + dt.timedelta(hours=10)
        monkeypatch.setattr(server_module, "_today_local", lambda: event_start.date())
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(event_start.date().isoformat(), duration_minutes=374)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["baseline_confirmed_at"] = (now + dt.timedelta(hours=1)).isoformat()

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

        assert result["status"] == "proposal"
        assert result["facts"]["personal_event_baseline"]["freshness"] == "stale"
        assert result["recommendation"] == {
            "maximum_additional_mg": 200,
            "suggested_additional_mg": None,
            "basis": "upper_bound_only",
        }
        assert result["reason"] == "personal_event_baseline_unavailable"

    @pytest.mark.parametrize("missing_field", ["cutoff_before_sleep_hours", "contraindications"])
    async def test_cutoff_and_contraindication_confirmation_are_required(
        self,
        mcp_client,
        store_factory,
        pinned_tz,
        missing_field,
    ):
        _, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        del args[missing_field]

        with pytest.raises(ToolError):
            await mcp_client.call_tool("get_caffeine_proposal", args)

    async def test_missing_intended_consumption_time_fails_closed(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["intended_consumption_at"] = None

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

        assert result["status"] == "insufficient_data"
        assert result["reason"] == "missing_timing"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert result["recommendation"]["suggested_additional_mg"] is None

    async def test_contraindication_returns_noop_without_a_numeric_proposal(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["contraindications"] = ["relevant_medication_or_condition"]

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

        assert result["status"] == "noop"
        assert result["reason"] == "clinician_guidance_required"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert result["recommendation"]["suggested_additional_mg"] is None

    async def test_late_intended_consumption_respects_sleep_cutoff(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["intended_consumption_at"] = (event_start + dt.timedelta(hours=5)).isoformat()

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

        assert result["status"] == "noop"
        assert result["reason"] == "within_sleep_cutoff"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert result["recommendation"]["suggested_additional_mg"] is None

    async def test_timezone_sensitive_inputs_require_explicit_offsets(
        self,
        mcp_client,
        store_factory,
        pinned_tz,
    ):
        _, event_start, _ = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )

        with pytest.raises(ToolError, match="explicit UTC offset"):
            await mcp_client.call_tool(
                "get_caffeine_proposal",
                {
                    "event_id": str(event_id),
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "target_sleep_at": "2026-08-02T23:00:00",
                },
            )
