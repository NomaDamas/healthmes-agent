"""Tests for the store-backed MCP tools (tasks / schedule / food / decisions).

Everything runs against the in-memory sqlite store; DB side effects are
verified through a direct session from the same factory.
"""

import datetime as dt
import uuid

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    DecisionRecord,
    FoodLog,
    ProposalStatus,
    ScheduleProposal,
    Task,
)

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
        with pytest.raises(ToolError, match="weekly_goal"):
            await mcp_client.call_tool(
                "upsert_task", {"title": "x", "goal_id": str(uuid.uuid4())}
            )
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


class TestScheduleTools:
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

    async def test_propose_blocks_validation(self, mcp_client, call_tool):
        with pytest.raises(ToolError, match="must not be empty"):
            await mcp_client.call_tool("propose_schedule_blocks", {"blocks": []})
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
