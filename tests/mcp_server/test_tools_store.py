"""Tests for the store-backed MCP tools (tasks / schedule / food / decisions).

Everything runs against the in-memory sqlite store; DB side effects are
verified through a direct session from the same factory.
"""

import datetime as dt
import uuid

import pytest
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy import select

from healthmes.api.auth import viewer_token
from healthmes.api.briefing import decision_viewer_url
from healthmes.calendars.adjustments import MAX_EVIDENCE_CLOCK_SKEW
from healthmes.calendars.base import ExternalEvent
from healthmes.config import Settings
from healthmes.mcp_server import server as server_module
from healthmes.store import (
    CalendarEventMirror,
    CalendarMutationProposal,
    CalendarMutationStatus,
    CalendarSource,
    DecisionRecord,
    EnergyDemand,
    FoodLog,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TriggerEvent,
)
from healthmes.trusted_session import issue_trusted_session_proof

TREE = {
    "type": "rule",
    "label": "readiness low",
    "detail": "sleep debt 20, hrv z -1.41",
    "children": [
        {"type": "input", "label": "sleep_debt=20"},
        {
            "type": "option",
            "label": "move deep work to tomorrow",
            "children": [{"type": "action", "label": "propose 09:00-11:00 block"}],
        },
    ],
}
OWNER_USER_ID = "owner-user"
OWNER_CHAT_ID = "owner-chat"


def calendar_reply_arguments(
    reply_handle: str,
    *,
    action: str = "적용",
    tool_name: str = "resolve_calendar_adjustment",
    user_id: str = OWNER_USER_ID,
    chat_id: str = OWNER_CHAT_ID,
    message_id: str = "message-1",
) -> dict[str, str]:
    response = f"{action} {reply_handle}"
    arguments = {
        "response": response,
        "reply_handle": reply_handle,
    }
    proof = issue_trusted_session_proof(
        "test-calendar-adjustment-secret-32-characters",
        tool_name=tool_name,
        arguments=arguments,
        platform="telegram",
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
    )
    return {**arguments, "trusted_session_proof": proof}


def test_adjustment_handle_secret_is_dedicated_and_fails_closed() -> None:
    unrelated_secrets = Settings(
        hermes_webhook_secret=SecretStr("webhook-secret"),
        api_token=SecretStr("api-token"),
        _env_file=None,
    )

    with pytest.raises(ToolError, match="HEALTHMES_CALENDAR_ADJUSTMENT_SECRET"):
        server_module._adjustment_handle_secret(unrelated_secrets)

    dedicated_secret = "a" * 64
    configured = Settings(
        calendar_adjustment_secret=SecretStr(dedicated_secret),
        hermes_webhook_secret=SecretStr("webhook-secret"),
        api_token=SecretStr("api-token"),
        _env_file=None,
    )
    assert server_module._adjustment_handle_secret(configured) == dedicated_secret


def test_telegram_owner_binding_is_explicit_and_fails_closed() -> None:
    with pytest.raises(ToolError, match="must bind one explicit owner"):
        server_module._telegram_owner_binding(Settings(_env_file=None))
    with pytest.raises(ToolError, match="must bind one explicit owner"):
        server_module._telegram_owner_binding(
            Settings(
                telegram_owner_user_id="*",
                telegram_owner_chat_id="owner-chat",
                _env_file=None,
            )
        )

    assert server_module._telegram_owner_binding(
        Settings(
            telegram_owner_user_id=OWNER_USER_ID,
            telegram_owner_chat_id=OWNER_CHAT_ID,
            _env_file=None,
        )
    ) == (OWNER_USER_ID, OWNER_CHAT_ID)


class TestUpsertAndListTasks:
    async def test_create_applies_documented_defaults(self, mcp_client, call_tool):
        result = await call_tool(mcp_client, "upsert_task", {"title": "Write weekly report"})
        assert result["created"] is True
        task = result["task"]
        assert task["energy_demand"] == "med"
        assert task["status"] == "todo"
        assert task["source"] == "agent"
        assert task["deadline"] is None

    async def test_create_with_fields_and_date_only_deadline(self, mcp_client, call_tool):
        result = await call_tool(
            mcp_client,
            "upsert_task",
            {
                "title": "Prepare talk",
                "est_minutes": 90,
                "deadline": "2026-07-15",
                "energy_demand": "high",
                "source": "user",
            },
        )
        task = result["task"]
        assert task["deadline"] == "2026-07-15T00:00:00+00:00"  # midnight UTC
        assert task["energy_demand"] == "high"
        assert task["source"] == "user"
        assert task["est_minutes"] == 90

    async def test_update_changes_only_provided_fields(
        self, mcp_client, call_tool, store_factory
    ):
        created = await call_tool(mcp_client, "upsert_task", {"title": "Refactor triggers"})
        task_id = created["task"]["id"]
        updated = await call_tool(
            mcp_client,
            "upsert_task",
            {"task_id": task_id, "status": "in_progress", "energy_demand": "medium"},
        )
        assert updated["created"] is False
        assert updated["task"]["status"] == "in_progress"
        assert updated["task"]["energy_demand"] == "med"  # 'medium' alias
        assert updated["task"]["title"] == "Refactor triggers"
        with store_factory() as session:
            row = session.get(Task, uuid.UUID(task_id))
            assert row is not None and row.status == "in_progress"

    async def test_validation_errors(self, mcp_client):
        with pytest.raises(ToolError, match="title is required"):
            await mcp_client.call_tool("upsert_task", {})
        with pytest.raises(ToolError, match="energy_demand"):
            await mcp_client.call_tool(
                "upsert_task", {"title": "x", "energy_demand": "extreme"}
            )
        with pytest.raises(ToolError, match="not found"):
            await mcp_client.call_tool(
                "upsert_task", {"task_id": str(uuid.uuid4()), "status": "done"}
            )
        # An unknown goal_id is lenient now (created with a note), not an error.
        unknown_goal = await mcp_client.call_tool(
            "upsert_task", {"title": "x", "goal_id": str(uuid.uuid4())}
        )
        assert unknown_goal.data["created"] is True
        assert "not found" in unknown_goal.data["goal_note"]
        with pytest.raises(ToolError, match="est_minutes"):
            await mcp_client.call_tool("upsert_task", {"title": "x", "est_minutes": 0})

    async def test_list_hides_done_by_default_and_sorts_by_deadline(
        self, mcp_client, call_tool
    ):
        await call_tool(
            mcp_client, "upsert_task", {"title": "later", "deadline": "2026-07-20"}
        )
        await call_tool(
            mcp_client, "upsert_task", {"title": "sooner", "deadline": "2026-07-10"}
        )
        await call_tool(mcp_client, "upsert_task", {"title": "no deadline"})
        done = await call_tool(mcp_client, "upsert_task", {"title": "finished"})
        await call_tool(
            mcp_client, "upsert_task", {"task_id": done["task"]["id"], "status": "done"}
        )

        listing = await call_tool(mcp_client, "list_tasks", {})
        assert [t["title"] for t in listing["tasks"]] == ["sooner", "later", "no deadline"]

        everything = await call_tool(mcp_client, "list_tasks", {"include_done": True})
        assert everything["count"] == 4
        only_done = await call_tool(mcp_client, "list_tasks", {"status": "done"})
        assert [t["title"] for t in only_done["tasks"]] == ["finished"]

    async def test_scheduled_status_is_first_class(self, mcp_client, call_tool):
        """'scheduled' is a REST state-machine status (api/tasks.py); the MCP
        write surface of the same table must accept and filter it — the
        planner marks a task 'scheduled' after its block is placed."""
        created = await call_tool(mcp_client, "upsert_task", {"title": "Place me"})
        updated = await call_tool(
            mcp_client,
            "upsert_task",
            {"task_id": created["task"]["id"], "status": "scheduled"},
        )
        assert updated["task"]["status"] == "scheduled"

        by_status = await call_tool(mcp_client, "list_tasks", {"status": "scheduled"})
        assert [t["title"] for t in by_status["tasks"]] == ["Place me"]
        # An open (non-terminal) status: visible in the default listing too.
        default_listing = await call_tool(mcp_client, "list_tasks", {})
        assert "Place me" in [t["title"] for t in default_listing["tasks"]]

    async def test_task_status_vocabulary_matches_rest_state_machine(self):
        """The two write surfaces of the task table must agree exactly."""
        from typing import get_args

        from healthmes.api.tasks import ALLOWED_TRANSITIONS, TaskStatus
        from healthmes.mcp_server.server import TASK_STATUSES
        from healthmes.store.enums import TASK_STATUSES as STORE_STATUSES

        assert TASK_STATUSES == STORE_STATUSES
        assert set(ALLOWED_TRANSITIONS) == STORE_STATUSES
        assert set(get_args(TaskStatus)) == STORE_STATUSES


class TestCalendarAdjustmentToolContract:
    async def test_resolver_schema_is_handle_only_and_server_proof_bound(
        self, mcp_client
    ):
        tools = await mcp_client.list_tools()
        resolver = next(tool for tool in tools if tool.name == "resolve_calendar_adjustment")
        properties = resolver.inputSchema["properties"]

        assert set(properties) == {
            "response",
            "reply_handle",
            "trusted_session_proof",
        }
        assert "proposal_id" not in properties
        assert "response_channel" not in properties

    async def test_schedule_resolver_schema_is_handle_only_and_server_proof_bound(
        self,
        mcp_client,
    ):
        tools = await mcp_client.list_tools()
        resolver = next(
            tool for tool in tools if tool.name == "resolve_schedule_proposal"
        )
        properties = resolver.inputSchema["properties"]

        assert set(properties) == {
            "response",
            "reply_handle",
            "trusted_session_proof",
        }
        assert "proposal_id" not in properties
        assert "action" not in properties


class TestScheduleTools:
    def test_afternoon_busy_minutes_uses_existing_noon_to_six_window(self, pinned_tz):
        day = dt.date(2026, 7, 24)
        local_start = dt.datetime.combine(day, dt.time(hour=17), tzinfo=pinned_tz)
        event = CalendarEventMirror(
            external_id="window-boundary",
            calendar_source=CalendarSource.GOOGLE,
            summary=None,
            start_at=local_start.astimezone(dt.UTC),
            end_at=(local_start + dt.timedelta(hours=2)).astimezone(dt.UTC),
        )

        busy = server_module._afternoon_busy_minutes([event], day, pinned_tz)

        assert busy == 60

    def _mirror_event(self, store_factory, start: dt.datetime, end: dt.datetime, summary: str):
        with store_factory() as session:
            session.add(
                CalendarEventMirror(
                    external_id=f"evt-{summary}",
                    calendar_source=CalendarSource.GOOGLE,
                    summary=summary,
                    start_at=start,
                    end_at=end,
                )
            )
            session.commit()

    async def test_propose_blocks_creates_proposals_and_flags_conflicts(
        self, mcp_client, call_tool, store_factory
    ):
        created = await call_tool(mcp_client, "upsert_task", {"title": "Deep work"})
        task_id = created["task"]["id"]
        tomorrow = dt.datetime.now(dt.UTC).replace(
            hour=9, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        self._mirror_event(
            store_factory, tomorrow, tomorrow + dt.timedelta(hours=1), "Standup"
        )

        result = await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "task_id": task_id,
                        "start": tomorrow.isoformat(),
                        "end": (tomorrow + dt.timedelta(hours=2)).isoformat(),
                    },
                    {
                        "task_id": task_id,
                        "start": (tomorrow + dt.timedelta(hours=3)).isoformat(),
                        "end": (tomorrow + dt.timedelta(hours=4)).isoformat(),
                    },
                ]
            },
        )
        first, second = result["proposals"]
        assert first["proposal_status"] == "proposed"
        assert first["task_title"] == "Deep work"
        assert [c["summary"] for c in first["conflicts"]] == ["Standup"]
        assert second["conflicts"] == []
        with store_factory() as session:
            rows = list(session.scalars(select(ScheduleProposal)))
            assert len(rows) == 2
            assert all(row.status == ProposalStatus.PROPOSED for row in rows)

    async def test_propose_planned_sleep_carries_explicit_calendar_kind(
        self, mcp_client, call_tool, store_factory
    ):
        start = dt.datetime.now(dt.UTC).replace(
            hour=23, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        result = await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "title": "Night rest",
                        "healthmes_kind": "planned_sleep",
                        "start": start.isoformat(),
                        "end": (start + dt.timedelta(hours=8)).isoformat(),
                    }
                ]
            },
        )

        assert result["proposals"][0]["healthmes_kind"] == "planned_sleep"
        with store_factory() as session:
            proposal = session.scalars(select(ScheduleProposal)).one()
            assert proposal.healthmes_kind == "planned_sleep"

    async def test_schedule_resolution_requires_exact_owner_bound_proof_and_is_one_time(
        self,
        mcp_client,
        call_tool,
        store_factory,
    ):
        start = dt.datetime.now(dt.UTC).replace(
            hour=9, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        proposed = await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "title": "Owner-approved block",
                        "start": start.isoformat(),
                        "end": (start + dt.timedelta(hours=1)).isoformat(),
                    }
                ]
            },
        )
        handle = proposed["proposals"][0]["reply_handle"]
        valid = calendar_reply_arguments(
            handle,
            tool_name="resolve_schedule_proposal",
        )
        proof = valid["trusted_session_proof"]
        tampered_proof = f"{proof[:-1]}{'0' if proof[-1] != '0' else '1'}"
        attempts = [
            {
                "response": f"적용 {handle}",
                "reply_handle": handle,
            },
            {**valid, "trusted_session_proof": tampered_proof},
            calendar_reply_arguments(
                handle,
                tool_name="resolve_schedule_proposal",
                user_id="different-user",
            ),
            calendar_reply_arguments(
                handle,
                tool_name="resolve_schedule_proposal",
                chat_id="different-chat",
            ),
            calendar_reply_arguments(
                handle,
                tool_name="resolve_calendar_adjustment",
            ),
        ]
        for arguments in attempts:
            with pytest.raises(ToolError, match="trusted_session_proof"):
                await mcp_client.call_tool(
                    "resolve_schedule_proposal",
                    arguments,
                )

        resolved = await call_tool(
            mcp_client,
            "resolve_schedule_proposal",
            valid,
        )
        assert resolved["proposal"]["proposal_status"] == "accepted"
        assert resolved["proposal"]["calendar_write"] == "queued"

        with pytest.raises(ToolError, match="already consumed"):
            await mcp_client.call_tool(
                "resolve_schedule_proposal",
                valid,
            )
        with store_factory() as session:
            proposal = session.scalars(select(ScheduleProposal)).one()
            assert proposal.status is ProposalStatus.ACCEPTED

    async def test_upsert_task_tolerates_non_uuid_goal_ref(self, mcp_client, call_tool):
        """An LLM often passes a human label for goal_id; the task is still
        created (not an error) and a note is returned (docs: live-E2E fix)."""
        result = await call_tool(
            mcp_client, "upsert_task", {"title": "논문 리뷰", "goal_id": "goal-1"}
        )
        assert result["created"] is True
        assert "goal-1" in result["goal_note"]
        assert result["task"]["goal_id"] is None

    async def test_propose_blocks_auto_creates_task_from_title(
        self, mcp_client, call_tool, store_factory
    ):
        """Blocks may carry a title instead of a task_id — a task is auto-created
        so the agent can propose a plan without the UUID round-trip."""
        start = dt.datetime.now(dt.UTC).replace(
            hour=9, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        result = await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "title": "발표 준비",
                        "energy_demand": "high",
                        "start": start.isoformat(),
                        "end": (start + dt.timedelta(minutes=90)).isoformat(),
                    }
                ]
            },
        )
        [proposal] = result["proposals"]
        assert proposal["task_title"] == "발표 준비"
        assert proposal["proposal_status"] == "proposed"

        # LLMs pass "" instead of null for task_id — treated as omitted.
        empty = await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "task_id": "",
                        "title": "저녁 러닝",
                        "start": (start + dt.timedelta(hours=8)).isoformat(),
                        "end": (start + dt.timedelta(hours=8, minutes=30)).isoformat(),
                    }
                ]
            },
        )
        assert empty["proposals"][0]["task_title"] == "저녁 러닝"
        with store_factory() as session:
            task = session.get(Task, uuid.UUID(proposal["task_id"]))
            assert task is not None and task.energy_demand == EnergyDemand.HIGH

    async def test_propose_blocks_validation(self, mcp_client, call_tool):
        with pytest.raises(ToolError, match="must not be empty"):
            await mcp_client.call_tool("propose_schedule_blocks", {"blocks": []})
        with pytest.raises(ToolError, match="either task_id or a non-empty title"):
            await mcp_client.call_tool(
                "propose_schedule_blocks",
                {"blocks": [{"start": "2026-07-17T09:00:00", "end": "2026-07-17T10:00:00"}]},
            )
        created = await call_tool(mcp_client, "upsert_task", {"title": "t"})
        with pytest.raises(ToolError, match="end must be after start"):
            await mcp_client.call_tool(
                "propose_schedule_blocks",
                {
                    "blocks": [
                        {
                            "task_id": created["task"]["id"],
                            "start": "2026-07-10T10:00:00Z",
                            "end": "2026-07-10T09:00:00Z",
                        }
                    ]
                },
            )
        with pytest.raises(ToolError, match="task .* not found"):
            await mcp_client.call_tool(
                "propose_schedule_blocks",
                {
                    "blocks": [
                        {
                            "task_id": str(uuid.uuid4()),
                            "start": "2026-07-10T09:00:00Z",
                            "end": "2026-07-10T10:00:00Z",
                        }
                    ]
                },
            )

    async def test_get_schedule_returns_window_events_and_pending_proposals(
        self, mcp_client, call_tool, store_factory, pinned_tz
    ):
        # Seed relative to the pinned *local* timezone: the window anchors at
        # local midnight (one "today" across all tools), so 14:00 tomorrow
        # local is deterministically inside 7d and outside 'today'.
        tomorrow = dt.datetime.now(pinned_tz).replace(
            hour=14, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        self._mirror_event(
            store_factory, tomorrow, tomorrow + dt.timedelta(hours=1), "Dentist"
        )
        far_future = tomorrow + dt.timedelta(days=30)
        self._mirror_event(
            store_factory, far_future, far_future + dt.timedelta(hours=1), "Far away"
        )
        created = await call_tool(mcp_client, "upsert_task", {"title": "Deep work"})
        await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "task_id": created["task"]["id"],
                        "start": (tomorrow + dt.timedelta(hours=2)).isoformat(),
                        "end": (tomorrow + dt.timedelta(hours=3)).isoformat(),
                    }
                ]
            },
        )

        result = await call_tool(mcp_client, "get_schedule", {"range": "7d"})
        assert result["window"]["days"] == 7
        assert [event["summary"] for event in result["events"]] == ["Dentist"]
        assert len(result["proposals"]) == 1
        assert result["proposals"][0]["task_title"] == "Deep work"

        today_only = await call_tool(mcp_client, "get_schedule", {"range": "today"})
        assert today_only["events"] == []

    async def test_get_schedule_projects_adjustment_eligibility_without_provider_ids(
        self, mcp_client, call_tool, store_factory, pinned_tz
    ):
        start = dt.datetime.now(pinned_tz).replace(
            hour=14, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        with store_factory() as session:
            session.add(
                CalendarEventMirror(
                    external_id="google-secret-event",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="Recovery focus",
                    start_at=start.astimezone(dt.UTC),
                    end_at=(start + dt.timedelta(hours=1)).astimezone(dt.UTC),
                    etag='"etag-v1"',
                    organizer_self=True,
                    has_attendees=False,
                    is_recurring=False,
                    event_type="default",
                    is_all_day=False,
                    is_locked=False,
                    status="confirmed",
                )
            )
            session.commit()

        result = await call_tool(mcp_client, "get_schedule", {"range": "7d"})
        [event] = result["events"]
        assert "external_id" not in event
        assert "etag" not in event
        assert event["adjustment"] == {
            "eligible": True,
            "operations": ["shorten"],
            "reasons": [],
        }

    async def test_evaluate_and_resolve_calendar_adjustment_are_redacted_and_confirmation_gated(
        self, mcp_client, call_tool, store_factory, pinned_tz, monkeypatch
    ):
        day = (dt.datetime.now(pinned_tz) + dt.timedelta(days=1)).date()
        first = dt.datetime.combine(day, dt.time(hour=14), tzinfo=pinned_tz)
        with store_factory() as session:
            for index in range(3):
                start = first + dt.timedelta(hours=index)
                session.add(
                    CalendarEventMirror(
                        external_id=f"google-secret-{index}",
                        calendar_source=CalendarSource.GOOGLE,
                        summary="Recovery focus",
                        start_at=start.astimezone(dt.UTC),
                        end_at=(start + dt.timedelta(hours=1)).astimezone(dt.UTC),
                        etag='"etag-v1"',
                        organizer_self=True,
                        has_attendees=False,
                        is_recurring=False,
                        event_type="default",
                        is_all_day=False,
                        is_locked=False,
                        status="confirmed",
                    )
                )
            session.commit()

        async def fake_readiness(date: str | None = None) -> dict:
            return {
                "status": "ok",
                "date": date,
                "sleep_debt": {
                    "status": "ok",
                    "confidence": "medium",
                    "date": day.isoformat(),
                },
                "hrv": {
                    "status": "ok",
                    "confidence": "medium",
                    "date": day.isoformat(),
                    "score": 35,
                },
                "charge": {
                    "status": "ok",
                    "confidence": "medium",
                    "date": day.isoformat(),
                    "value": 35,
                },
            }

        class FakeWriter:
            def __init__(self) -> None:
                self.changes = []

            def apply_confirmed_external_time_change(self, change):
                self.changes.append(change)
                return ExternalEvent(
                    external_id=change.external_event_id,
                    summary="Recovery focus",
                    start_at=change.proposed_start_at,
                    end_at=change.proposed_end_at,
                    etag='"etag-v2"',
                    organizer_self=True,
                    has_attendees=False,
                    is_recurring=False,
                    event_type="default",
                    is_all_day=False,
                    is_locked=False,
                    status="confirmed",
                )

        writer = FakeWriter()
        monkeypatch.setattr(server_module, "get_daily_readiness_context", fake_readiness)
        server_module.set_calendar_adjustment_writer(writer)

        evaluated = await call_tool(
            mcp_client, "evaluate_morning_calendar_nudge", {"date": day.isoformat()}
        )
        assert evaluated["outcome"] == "proposed"
        assert evaluated["reply_handle"]
        assert evaluated["display"]["event_label"] == "Recovery focus"
        assert evaluated["display"]["before"]["end"].endswith("+09:00")
        assert "google-secret" not in str(evaluated)
        assert '"etag-v1"' not in str(evaluated)
        assert writer.changes == []

        with pytest.raises(ToolError, match="trusted_session_proof"):
            await mcp_client.call_tool(
                "resolve_calendar_adjustment",
                {
                    "response": f"적용 {evaluated['reply_handle']}",
                    "reply_handle": evaluated["reply_handle"],
                },
            )
        invalid_arguments = calendar_reply_arguments("not-the-issued-handle")
        with pytest.raises(ToolError, match="invalid, expired, or already consumed"):
            await mcp_client.call_tool(
                "resolve_calendar_adjustment",
                invalid_arguments,
            )
        assert writer.changes == []

        deduped = await call_tool(
            mcp_client, "evaluate_morning_calendar_nudge", {"date": day.isoformat()}
        )
        assert deduped == {"status": "ok", "outcome": "deduplicated", "date": day.isoformat()}
        assert "reply_handle" not in deduped

        with store_factory() as session:
            [proposal] = list(session.scalars(select(CalendarMutationProposal)))
            assert proposal.reply_handle_digest != evaluated["reply_handle"]
            [trigger] = list(session.scalars(select(TriggerEvent)))
            assert trigger.payload["outcome"] == "proposed"
            tree_text = str(session.get(DecisionRecord, proposal.proposal_decision_record_id).tree)
            assert evaluated["reply_handle"] not in tree_text
            assert proposal.reply_handle_digest not in tree_text
            assert "google-secret" not in tree_text
            assert '"etag-v1"' not in tree_text

        declined = await call_tool(
            mcp_client,
            "resolve_calendar_adjustment",
            calendar_reply_arguments(evaluated["reply_handle"], action="그대로"),
        )
        assert declined["status"] == CalendarMutationStatus.DECLINED.value
        assert declined["receipt"] == {
            "operation": "shorten",
            "delta_minutes": 30,
            "status": "declined",
            "provider_code": "user_declined",
        }
        assert writer.changes == []

    async def test_evaluate_rejects_same_local_date_future_sleep_score(
        self, mcp_client, call_tool, mcp_env, store_factory, pinned_tz
    ):
        now_local = dt.datetime.now(pinned_tz)
        day = (now_local + dt.timedelta(days=1)).date()
        future_recorded_at = (
            dt.datetime.combine(
                day,
                dt.time.min,
                tzinfo=pinned_tz,
            )
            + MAX_EVIDENCE_CLOCK_SKEW
            + dt.timedelta(seconds=1)
        )
        assert future_recorded_at.astimezone(dt.UTC) > (
            dt.datetime.now(dt.UTC) + MAX_EVIDENCE_CLOCK_SKEW
        )
        for days_ago, score in enumerate((70, 80, 90, 90, 90)):
            score_day = day - dt.timedelta(days=days_ago)
            recorded_at = (
                future_recorded_at
                if days_ago == 0
                else dt.datetime.combine(
                    score_day,
                    dt.time(hour=7),
                    tzinfo=pinned_tz,
                )
            )
            mcp_env.add_score(
                "sleep",
                "internal",
                recorded_at.isoformat(),
                score,
            )
        mcp_env.add_score(
            "body_battery",
            "garmin",
            now_local.isoformat(),
            35,
        )

        event_start = dt.datetime.combine(
            day,
            dt.time(hour=14),
            tzinfo=pinned_tz,
        )
        with store_factory() as session:
            session.add(
                CalendarEventMirror(
                    external_id="future-sleep-target",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="Recovery focus",
                    start_at=event_start.astimezone(dt.UTC),
                    end_at=(event_start + dt.timedelta(hours=3)).astimezone(dt.UTC),
                    etag='"etag-v1"',
                    organizer_self=True,
                    has_attendees=False,
                    is_recurring=False,
                    event_type="default",
                    is_all_day=False,
                    is_locked=False,
                    status="confirmed",
                )
            )
            session.commit()

        readiness = await call_tool(
            mcp_client,
            "get_daily_readiness_context",
            {"date": day.isoformat()},
        )
        assert readiness["sleep_debt"]["last_night"]["recorded_at"] == (
            future_recorded_at.isoformat()
        )

        result = await call_tool(
            mcp_client,
            "evaluate_morning_calendar_nudge",
            {"date": day.isoformat()},
        )

        assert result["outcome"] == "no_action"
        assert result["reason"] == "future_sleep"

    async def test_no_action_returns_authenticated_decision_viewer_without_proposal(
        self, mcp_client, call_tool, store_factory, pinned_tz, monkeypatch
    ):
        day = (dt.datetime.now(pinned_tz) + dt.timedelta(days=1)).date()
        first = dt.datetime.combine(day, dt.time(hour=13), tzinfo=pinned_tz)
        with store_factory() as session:
            for index in range(3):
                start = first + dt.timedelta(hours=index)
                session.add(
                    CalendarEventMirror(
                        external_id=f"ineligible-secret-{index}",
                        calendar_source=CalendarSource.GOOGLE,
                        summary="Private busy block",
                        start_at=start.astimezone(dt.UTC),
                        end_at=(start + dt.timedelta(hours=1)).astimezone(dt.UTC),
                        etag='"private-etag"',
                        organizer_self=True,
                        has_attendees=True,
                        is_recurring=False,
                        event_type="default",
                        is_all_day=False,
                        is_locked=False,
                        status="confirmed",
                    )
                )
            session.commit()

        async def fake_readiness(date: str | None = None) -> dict:
            return {
                "status": "ok",
                "date": date,
                "sleep_debt": {
                    "status": "ok",
                    "confidence": "medium",
                    "date": day.isoformat(),
                },
                "charge": {
                    "status": "ok",
                    "confidence": "medium",
                    "date": day.isoformat(),
                    "value": 35,
                },
            }

        monkeypatch.setattr(server_module, "get_daily_readiness_context", fake_readiness)

        result = await call_tool(
            mcp_client, "evaluate_morning_calendar_nudge", {"date": day.isoformat()}
        )

        assert result["outcome"] == "no_action"
        assert result["reason"] == "no_eligible_event"
        assert result["display"] == "오늘은 회복 상태와 일정 조건에 맞는 단축 제안이 없습니다."
        assert result["decision_viewer_url"].startswith("http://healthmes.test:8100/decisions/")
        assert "reply_handle" not in result
        assert "ineligible-secret" not in str(result)
        assert "private-etag" not in str(result)
        with store_factory() as session:
            [trigger] = list(session.scalars(select(TriggerEvent)))
            decision = session.get(DecisionRecord, uuid.UUID(trigger.payload["decision_record_id"]))
            assert decision is not None
            assert decision.tree["detail"]["reason"] == "no_eligible_event"
            assert list(session.scalars(select(CalendarMutationProposal))) == []

    @pytest.mark.parametrize(
        ("internal_status", "public_status"),
        [
            (CalendarMutationStatus.APPLIED_RECOVERED, CalendarMutationStatus.APPLIED.value),
            (CalendarMutationStatus.UNKNOWN, CalendarMutationStatus.UNKNOWN.value),
            (CalendarMutationStatus.FAILED_NO_CHANGE, CalendarMutationStatus.FAILED.value),
        ],
    )
    def test_internal_recovery_statuses_are_not_exposed_by_mcp(
        self, internal_status, public_status
    ):
        assert server_module._public_calendar_adjustment_status(internal_status) == public_status

    async def test_resolve_rejects_unknown_handle_without_sensitive_detail(
        self, mcp_client
    ):
        with pytest.raises(ToolError, match="invalid, expired, or already consumed"):
            await mcp_client.call_tool(
                "resolve_calendar_adjustment",
                calendar_reply_arguments("not-a-live-handle"),
            )

    async def test_resolve_calendar_adjustment_yes_calls_injected_writer_once(
        self, mcp_client, call_tool, store_factory, pinned_tz, monkeypatch
    ):
        day = (dt.datetime.now(pinned_tz) + dt.timedelta(days=1)).date()
        start = dt.datetime.combine(day, dt.time(hour=14), tzinfo=pinned_tz)
        with store_factory() as session:
            session.add(
                CalendarEventMirror(
                    external_id="google-secret-target",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="Recovery focus",
                    start_at=start.astimezone(dt.UTC),
                    end_at=(start + dt.timedelta(hours=3)).astimezone(dt.UTC),
                    etag='"etag-v1"',
                    organizer_self=True,
                    has_attendees=False,
                    is_recurring=False,
                    event_type="default",
                    is_all_day=False,
                    is_locked=False,
                    status="confirmed",
                )
            )
            session.commit()

        async def fake_readiness(date: str | None = None) -> dict:
            return {
                "sleep_debt": {"status": "ok", "confidence": "medium", "date": day.isoformat()},
                "hrv": {
                    "status": "ok",
                    "confidence": "medium",
                    "date": day.isoformat(),
                },
                "charge": {
                    "status": "ok",
                    "confidence": "medium",
                    "entries": [
                        {
                            "category": "body_battery",
                            "provider": "garmin",
                            "value": 30,
                            "observed_on": day.isoformat(),
                        }
                    ],
                },
            }

        class FakeWriter:
            def __init__(self) -> None:
                self.changes = []

            def apply_confirmed_external_time_change(self, change):
                self.changes.append(change)
                return ExternalEvent(
                    external_id=change.external_event_id,
                    summary="Recovery focus",
                    start_at=change.proposed_start_at,
                    end_at=change.proposed_end_at,
                    etag='"etag-v2"',
                    organizer_self=True,
                    has_attendees=False,
                    is_recurring=False,
                    event_type="default",
                    is_all_day=False,
                    is_locked=False,
                    status="confirmed",
                )

        writer = FakeWriter()
        monkeypatch.setattr(server_module, "get_daily_readiness_context", fake_readiness)
        server_module.set_calendar_adjustment_writer(writer)
        evaluated = await call_tool(
            mcp_client, "evaluate_morning_calendar_nudge", {"date": day.isoformat()}
        )
        assert evaluated["display"]["reply_options"] == [
            f"적용 {evaluated['reply_handle']}",
            f"그대로 {evaluated['reply_handle']}",
        ]
        assert evaluated["display"]["evidence"]["recovery_value_bucket"] == "low"
        assert evaluated["display"]["limitations"] == [
            "technical_eligibility_only",
            "explicit_confirmation_required",
        ]

        confirmation = calendar_reply_arguments(evaluated["reply_handle"])
        applied = await call_tool(
            mcp_client,
            "resolve_calendar_adjustment",
            confirmation,
        )
        assert applied["status"] == CalendarMutationStatus.APPLIED.value
        assert applied["receipt"]["provider_result"] == "matched"
        assert len(writer.changes) == 1
        change = writer.changes[0]
        assert change.external_event_id == "google-secret-target"
        assert change.proposed_start_at == change.original_start_at
        assert change.original_end_at - change.proposed_end_at == dt.timedelta(minutes=30)

        with pytest.raises(ToolError, match="already consumed"):
            await mcp_client.call_tool(
                "resolve_calendar_adjustment",
                confirmation,
            )
        assert len(writer.changes) == 1

    async def test_resolve_rejects_forged_or_non_owner_telegram_proof(
        self, mcp_client, call_tool, store_factory, pinned_tz, monkeypatch
    ):
        day = (dt.datetime.now(pinned_tz) + dt.timedelta(days=1)).date()
        start = dt.datetime.combine(day, dt.time(hour=14), tzinfo=pinned_tz)
        with store_factory() as session:
            session.add(
                CalendarEventMirror(
                    external_id="google-owner-proof-target",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="Recovery focus",
                    start_at=start.astimezone(dt.UTC),
                    end_at=(start + dt.timedelta(hours=3)).astimezone(dt.UTC),
                    etag='"etag-v1"',
                    organizer_self=True,
                    has_attendees=False,
                    is_recurring=False,
                    event_type="default",
                    is_all_day=False,
                    is_locked=False,
                    status="confirmed",
                )
            )
            session.commit()

        async def fake_readiness(date: str | None = None) -> dict:
            return {
                "sleep_debt": {
                    "status": "ok",
                    "confidence": "medium",
                    "last_night": {"date": day.isoformat(), "score": 70},
                },
                "charge": {
                    "status": "ok",
                    "confidence": "medium",
                    "date": day.isoformat(),
                    "value": 35,
                },
            }

        class FakeWriter:
            def __init__(self) -> None:
                self.changes = []

            def apply_confirmed_external_time_change(self, change):
                self.changes.append(change)
                raise AssertionError("untrusted proof reached the writer")

        writer = FakeWriter()
        monkeypatch.setattr(server_module, "get_daily_readiness_context", fake_readiness)
        server_module.set_calendar_adjustment_writer(writer)
        evaluated = await call_tool(
            mcp_client, "evaluate_morning_calendar_nudge", {"date": day.isoformat()}
        )
        handle = evaluated["reply_handle"]

        attempts = [
            {
                "response": f"적용 {handle}",
                "reply_handle": handle,
            },
            {
                **calendar_reply_arguments(handle),
                "trusted_session_proof": "forged.proof",
            },
            calendar_reply_arguments(handle, user_id="different-user"),
            calendar_reply_arguments(handle, chat_id="different-chat"),
        ]
        for arguments in attempts:
            with pytest.raises(ToolError, match="trusted_session_proof"):
                await mcp_client.call_tool("resolve_calendar_adjustment", arguments)
        assert writer.changes == []


class TestCaptureTools:
    async def test_log_food_persists_row(self, mcp_client, call_tool, store_factory):
        result = await call_tool(
            mcp_client,
            "log_food",
            {
                "description": "Bibimbap with extra vegetables",
                "logged_at": "2026-07-08T12:30:00Z",
                "meal_type": "lunch",
                "media_path": "media/2026-07-08/lunch.jpg",
                "source": "telegram",
            },
        )
        assert result["status"] == "ok"
        assert result["logged_at"] == "2026-07-08T12:30:00+00:00"
        with store_factory() as session:
            row = session.get(FoodLog, uuid.UUID(result["food_log_id"]))
            assert row is not None
            assert row.description == "Bibimbap with extra vegetables"
            assert row.meal_type == "lunch"
            assert row.media_path == "media/2026-07-08/lunch.jpg"

    async def test_log_food_validation(self, mcp_client):
        with pytest.raises(ToolError, match="description"):
            await mcp_client.call_tool("log_food", {"description": "   "})
        with pytest.raises(ToolError, match="meal_type"):
            await mcp_client.call_tool(
                "log_food", {"description": "toast", "meal_type": "brunch"}
            )

    async def test_record_decision_returns_viewer_url(
        self, mcp_client, call_tool, store_factory
    ):
        result = await call_tool(
            mcp_client,
            "record_decision",
            {
                "kind": "schedule_change",
                "summary": "Moved deep work to tomorrow morning due to low readiness.",
                "tree": TREE,
                "llm_model": "claude-x",
                "tokens": 1234,
            },
        )
        decision_id = result["decision_id"]
        assert result["viewer_url"] == f"http://healthmes.test:8100/decisions/{decision_id}"
        with store_factory() as session:
            row = session.get(DecisionRecord, uuid.UUID(decision_id))
            assert row is not None
            assert row.tree["children"][0]["label"] == "sleep_debt=20"
            assert row.llm_model == "claude-x"

    async def test_record_decision_viewer_url_embeds_the_derived_token(
        self, mcp_client, call_tool, tmp_path
    ):
        """Token-configured instance: the MCP link must stay byte-identical to
        the API construction (healthmes.api.auth.viewer_url is the single
        copy) — derived read-only credential embedded, never the API token."""
        token = "mcp-viewer-link-test-token"
        secured = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            public_base_url="http://healthmes.test:8100",
            api_token=SecretStr(token),
            data_dir=tmp_path / "data-secured",
            scheduler_enabled=False,
            _env_file=None,
        )
        server_module.set_settings(secured)  # mcp_env teardown resets this

        result = await call_tool(
            mcp_client,
            "record_decision",
            {"kind": "alert", "summary": "tokenized viewer link", "tree": TREE},
        )
        decision_id = result["decision_id"]
        assert result["viewer_url"] == decision_viewer_url(secured, decision_id)
        assert result["viewer_url"].endswith(f"?token={viewer_token(token)}")
        assert token not in result["viewer_url"]

    async def test_record_decision_links_to_proposals(self, mcp_client, call_tool):
        decision = await call_tool(
            mcp_client,
            "record_decision",
            {"kind": "schedule_change", "summary": "plan", "tree": TREE},
        )
        created = await call_tool(mcp_client, "upsert_task", {"title": "Deep work"})
        result = await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "task_id": created["task"]["id"],
                        "start": "2026-07-10T09:00:00Z",
                        "end": "2026-07-10T10:00:00Z",
                    }
                ],
                "decision_record_id": decision["decision_id"],
            },
        )
        assert result["status"] == "ok"

    async def test_record_alert_decision_binds_exact_trigger_once(
        self,
        mcp_client,
        call_tool,
        store_factory,
    ):
        trigger = TriggerEvent(
            fired_at=dt.datetime(2026, 7, 9, 14, 0, tzinfo=dt.UTC),
            rule_id="calendar_task_intake",
            payload={"summary": "Schedule this task"},
            alert_sent=True,
            dedup_key="calendar-task:correlation-test",
        )
        with store_factory() as session:
            session.add(trigger)
            session.commit()
            trigger_id = trigger.id

        start = dt.datetime.now(dt.UTC).replace(
            hour=9, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        proposed = await call_tool(
            mcp_client,
            "propose_schedule_blocks",
            {
                "blocks": [
                    {
                        "title": "Correlated alert block",
                        "start": start.isoformat(),
                        "end": (start + dt.timedelta(hours=1)).isoformat(),
                    }
                ]
            },
        )
        proposal_id = proposed["proposals"][0]["id"]
        result = await call_tool(
            mcp_client,
            "record_decision",
            {
                "kind": "alert",
                "summary": "Correlated alert",
                "tree": TREE,
                "trigger_event_id": str(trigger_id),
                "schedule_proposal_ids": [proposal_id],
            },
        )
        assert result["schedule_proposal_ids"] == [proposal_id]
        with store_factory() as session:
            row = session.get(DecisionRecord, uuid.UUID(result["decision_id"]))
            assert row is not None
            assert row.trigger_event_id == trigger_id
            proposal = session.get(ScheduleProposal, uuid.UUID(proposal_id))
            assert proposal is not None
            assert proposal.decision_record_id == row.id

        with pytest.raises(ToolError, match="already has a decision"):
            await mcp_client.call_tool(
                "record_decision",
                {
                    "kind": "alert",
                    "summary": "Duplicate correlation",
                    "tree": TREE,
                    "trigger_event_id": str(trigger_id),
                },
            )
        with pytest.raises(ToolError, match="valid only for kind='alert'"):
            await mcp_client.call_tool(
                "record_decision",
                {
                    "kind": "insight",
                    "summary": "Wrong kind",
                    "tree": TREE,
                    "trigger_event_id": str(trigger_id),
                },
            )
        with pytest.raises(ToolError, match="not found"):
            await mcp_client.call_tool(
                "record_decision",
                {
                    "kind": "alert",
                    "summary": "Missing trigger",
                    "tree": TREE,
                    "trigger_event_id": str(uuid.uuid4()),
                },
            )

    async def test_record_decision_tree_validation(self, mcp_client):
        with pytest.raises(ToolError, match="kind"):
            await mcp_client.call_tool(
                "record_decision", {"kind": "vibe", "summary": "s", "tree": TREE}
            )
        with pytest.raises(ToolError, match="node type"):
            await mcp_client.call_tool(
                "record_decision",
                {
                    "kind": "alert",
                    "summary": "s",
                    "tree": {"type": "wat", "label": "x"},
                },
            )
        with pytest.raises(ToolError, match="label"):
            await mcp_client.call_tool(
                "record_decision",
                {
                    "kind": "alert",
                    "summary": "s",
                    "tree": {"type": "rule", "label": "ok", "children": [{"type": "input"}]},
                },
            )
