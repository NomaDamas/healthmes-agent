"""Monthly/weekly goal MCP tools (list_goals / upsert_goal — PLAN §14).

The month layer answers "why this week": weekly goals nest under monthly
parents, lenient title refs mirror upsert_task's goal handling, and unknown
refs degrade to notes instead of errors.
"""

from sqlalchemy import select

from healthmes.store import MonthlyGoal, WeeklyGoal


class TestUpsertGoal:
    async def test_create_monthly_defaults_to_current_month(self, mcp_client, call_tool):
        result = await call_tool(
            mcp_client, "upsert_goal", {"scope": "monthly", "title": "출시 준비"}
        )
        goal = result["goal"]
        assert goal["scope"] == "monthly"
        assert goal["period_start"].endswith("-01")
        assert goal["status"] == "active"

    async def test_weekly_links_to_monthly_by_title(self, mcp_client, call_tool):
        await call_tool(mcp_client, "upsert_goal", {"scope": "monthly", "title": "출시 준비"})
        result = await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "weekly", "title": "베타 배포", "monthly_goal_ref": "출시 준비"},
        )
        assert result["goal"]["monthly_goal_id"] is not None
        assert "goal_note" not in result

    async def test_unknown_monthly_ref_saves_with_note(self, mcp_client, call_tool):
        result = await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "weekly", "title": "베타 배포", "monthly_goal_ref": "없는 목표"},
        )
        assert result["goal"]["monthly_goal_id"] is None
        assert "not found" in result["goal_note"]

    async def test_update_status_by_id(self, mcp_client, call_tool, store_factory):
        created = await call_tool(
            mcp_client, "upsert_goal", {"scope": "monthly", "title": "출시 준비"}
        )
        await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "monthly", "goal_id": created["goal"]["goal_id"], "status": "done"},
        )
        with store_factory() as session:
            row = session.scalars(select(MonthlyGoal)).one()
            assert row.status == "done"

    async def test_scope_and_status_validation(self, mcp_client, call_tool):
        import pytest
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="scope"):
            await call_tool(mcp_client, "upsert_goal", {"scope": "daily", "title": "x"})
        with pytest.raises(ToolError, match="status"):
            await call_tool(
                mcp_client,
                "upsert_goal",
                {"scope": "weekly", "title": "x", "status": "paused"},
            )


class TestListGoals:
    async def test_nesting_and_unassigned(self, mcp_client, call_tool):
        await call_tool(mcp_client, "upsert_goal", {"scope": "monthly", "title": "출시 준비"})
        await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "weekly", "title": "베타 배포", "monthly_goal_ref": "출시 준비"},
        )
        await call_tool(mcp_client, "upsert_goal", {"scope": "weekly", "title": "운동 3회"})

        listing = await call_tool(mcp_client, "list_goals", {})
        [month] = listing["monthly_goals"]
        assert month["title"] == "출시 준비"
        assert [g["title"] for g in month["weekly_goals"]] == ["베타 배포"]
        assert [g["title"] for g in listing["unassigned_weekly"]] == ["운동 3회"]

    async def test_done_hidden_unless_included(self, mcp_client, call_tool, store_factory):
        created = await call_tool(
            mcp_client, "upsert_goal", {"scope": "weekly", "title": "끝난 목표"}
        )
        await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "weekly", "goal_id": created["goal"]["goal_id"], "status": "done"},
        )
        default = await call_tool(mcp_client, "list_goals", {})
        assert default["unassigned_weekly"] == []
        included = await call_tool(mcp_client, "list_goals", {"include_done": True})
        assert [g["title"] for g in included["unassigned_weekly"]] == ["끝난 목표"]
        with store_factory() as session:
            assert session.scalars(select(WeeklyGoal)).one().status == "done"


class TestReviewRegressions:
    async def test_ambiguous_monthly_title_requires_uuid(self, mcp_client, call_tool):
        await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "monthly", "title": "출시 준비", "period_start": "2026-06-01"},
        )
        await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "monthly", "title": "출시 준비", "period_start": "2026-07-01"},
        )
        result = await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "weekly", "title": "베타", "monthly_goal_ref": "출시 준비"},
        )
        assert result["goal"]["monthly_goal_id"] is None
        assert "2 monthly goals" in result["goal_note"]

    async def test_failed_relink_keeps_existing_link_and_says_so(
        self, mcp_client, call_tool
    ):
        await call_tool(mcp_client, "upsert_goal", {"scope": "monthly", "title": "출시 준비"})
        created = await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "weekly", "title": "베타", "monthly_goal_ref": "출시 준비"},
        )
        linked_id = created["goal"]["monthly_goal_id"]
        relink = await call_tool(
            mcp_client,
            "upsert_goal",
            {
                "scope": "weekly",
                "goal_id": created["goal"]["goal_id"],
                "monthly_goal_ref": "없는 목표",
            },
        )
        assert relink["goal"]["monthly_goal_id"] == linked_id  # unchanged
        assert "existing month link unchanged" in relink["goal_note"]

    async def test_monthly_period_normalized_to_first_day(self, mcp_client, call_tool):
        result = await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "monthly", "title": "중순 시작", "period_start": "2026-07-15"},
        )
        assert result["goal"]["period_start"] == "2026-07-01"

    async def test_weekly_under_done_month_is_not_misfiled(self, mcp_client, call_tool):
        month = await call_tool(
            mcp_client, "upsert_goal", {"scope": "monthly", "title": "끝난 달"}
        )
        await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "weekly", "title": "이어지는 주", "monthly_goal_ref": "끝난 달"},
        )
        await call_tool(
            mcp_client,
            "upsert_goal",
            {"scope": "monthly", "goal_id": month["goal"]["goal_id"], "status": "done"},
        )
        listing = await call_tool(mcp_client, "list_goals", {})
        assert listing["unassigned_weekly"] == []
        [entry] = listing["weekly_under_hidden_months"]
        assert entry["title"] == "이어지는 주"
        assert entry["monthly_goal_id"] is not None
