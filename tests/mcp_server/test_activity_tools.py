import datetime as dt
import json

from healthmes.activity.aggregation import rebuild_day_summaries
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    AppIntervalRecord,
)
from healthmes.activity.service import ingest_activity_batch


def _seed_activity(store_factory, pinned_tz) -> None:
    with store_factory() as session:
        ingest_activity_batch(
            session,
            ActivityBatchIn(
                source_provider="mcp-test-desktop",
                source_device="mcp-desktop-1",
                platform=ActivityPlatform.MACOS,
                capability=ActivityCapability.DETAILED,
                timezone="Asia/Seoul",
                records=[
                    AppIntervalRecord(
                        source_record_id="mcp-idle-before",
                        start_at=dt.datetime(2026, 7, 31, 15, tzinfo=dt.UTC),
                        end_at=dt.datetime(2026, 8, 1, 1, tzinfo=dt.UTC),
                        state="idle",
                    ),
                    AppIntervalRecord(
                        source_record_id="mcp-activity",
                        start_at=dt.datetime(2026, 8, 1, 1, tzinfo=dt.UTC),
                        end_at=dt.datetime(2026, 8, 1, 2, tzinfo=dt.UTC),
                        state="active",
                        app_id="private.mcp.app",
                        category="productivity",
                        launches=3,
                    ),
                    AppIntervalRecord(
                        source_record_id="mcp-idle-after",
                        start_at=dt.datetime(2026, 8, 1, 2, tzinfo=dt.UTC),
                        end_at=dt.datetime(2026, 8, 1, 15, tzinfo=dt.UTC),
                        state="idle",
                    ),
                ],
            ),
            rebuild_summaries=False,
        )
        rebuild_day_summaries(
            session,
            day=dt.date(2026, 8, 1),
            timezone=pinned_tz,
        )
        session.commit()


async def test_activity_context_tools_return_identity_free_stored_summaries(
    mcp_client,
    call_tool,
    store_factory,
    pinned_tz,
) -> None:
    _seed_activity(store_factory, pinned_tz)

    summary = await call_tool(
        mcp_client,
        "get_activity_summary",
        {"date": "2026-08-01"},
    )
    focus = await call_tool(
        mcp_client,
        "get_focus_context",
        {
            "start": "2026-08-01T01:00:00Z",
            "end": "2026-08-01T02:00:00Z",
        },
    )
    overwork = await call_tool(
        mcp_client,
        "get_overwork_context",
        {"date": "2026-08-01", "lookback_days": 7},
    )

    assert summary["status"] == "ok"
    assert summary["timezone"] == "UTC+09:00"
    assert summary["total_active_minutes"] == 60.0
    assert focus["status"] == "ok"
    assert focus["metrics"]["total_active_minutes"] == 60.0
    assert overwork["status"] == "ok"
    assert overwork["risk_level"] == "not_elevated"
    serialized = json.dumps({"summary": summary, "focus": focus, "overwork": overwork})
    assert "private.mcp.app" not in serialized


async def test_resolver_tool_selects_only_activity_for_activity_summary(
    mcp_client,
    call_tool,
    store_factory,
    pinned_tz,
) -> None:
    _seed_activity(store_factory, pinned_tz)

    result = await call_tool(
        mcp_client,
        "resolve_wellness_context",
        {
            "question_kind": "activity_summary",
            "date": "2026-08-01",
        },
    )

    assert result["status"] == "ok"
    assert result["selected_domains"] == ["activity"]
    assert result["not_selected_domains"] == [
        "wearable",
        "calendar",
        "nutrition",
        "time",
    ]
    assert result["contexts"]["activity"]["total_active_minutes"] == 60.0
    assert result["boundaries"] == [
        "specialized_policy_numbers_are_not_recomputed",
        "missing_data_is_not_zero",
        "association_is_not_causation",
        "context_only_not_a_final_wellness_decision",
    ]
