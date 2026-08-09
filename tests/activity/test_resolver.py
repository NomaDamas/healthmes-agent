import json
from datetime import UTC, date, datetime

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityContextResolveRequest,
    ActivityPlatform,
    AppIntervalRecord,
)
from healthmes.activity.resolver import resolve_wellness_context
from healthmes.activity.service import ingest_activity_batch


def _seed_activity(session) -> None:
    ingest_activity_batch(
        session,
        ActivityBatchIn(
            source_provider="test-desktop",
            source_device="desktop-1",
            platform=ActivityPlatform.MACOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            records=[
                AppIntervalRecord(
                    source_record_id="resolver-activity",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="private.app.identity",
                    category="productivity",
                    launches=2,
                )
            ],
        ),
    )


async def test_single_domain_request_does_not_read_unselected_wearables(session) -> None:
    _seed_activity(session)

    async def must_not_run(day: date):
        raise AssertionError(f"wearable reader must not run for {day}")

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="activity_summary",
            date="2026-08-01",
        ),
        default_timezone="UTC",
        wearable_reader=must_not_run,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
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
    assert "private.app.identity" not in json.dumps(result)


async def test_one_domain_failure_does_not_erase_other_focus_context(session) -> None:
    _seed_activity(session)

    async def broken_wearable(day: date):
        raise RuntimeError(f"wearable offline for {day}")

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=broken_wearable,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["status"] == "partial"
    assert result["selected_domains"] == ["activity", "wearable", "calendar"]
    assert result["domain_statuses"]["activity"] == "ok"
    assert result["contexts"]["wearable"]["status"] == "unavailable"
    assert result["contexts"]["wearable"]["reason"] == "RuntimeError"
    assert result["contexts"]["calendar"]["status"] == "insufficient_data"
    assert result["evidence"]
    assert result["freshness"]["activity"]["status"] == "stored_summary"
    assert "open_wearables_context_unavailable" in result["limitations"]


async def test_caffeine_for_focus_selects_bounded_cross_domain_context(session) -> None:
    _seed_activity(session)

    async def wearable(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "sleep": {"status": "ok", "duration_minutes": 420},
            "evidence_ids": ["wearable-readiness-1"],
            "freshness": {
                "recorded_at": "2026-08-01T08:00:00+00:00",
                "status": "open_wearables_summary",
            },
            "coverage": {"status": "partial", "ratio": 0.8},
            "limitations": [],
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="caffeine_for_focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
            lookback_days=7,
        ),
        default_timezone="UTC",
        wearable_reader=wearable,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["selected_domains"] == [
        "activity",
        "wearable",
        "calendar",
        "nutrition",
        "time",
    ]
    assert result["contexts"]["activity"]["focus"]["status"] == "ok"
    assert result["contexts"]["nutrition"]["kind"] == "confirmed_caffeine_ledger"
    assert result["contexts"]["nutrition"]["context"]["confirmed_caffeine_mg"] == 0.0
    assert result["contexts"]["nutrition"]["status"] == "incomplete"
    assert result["contexts"]["time"]["local_now"] == "2026-08-01T12:00:00+00:00"
    assert {
        "specialized_policy_numbers_are_not_recomputed",
        "missing_data_is_not_zero",
        "association_is_not_causation",
        "context_only_not_a_final_wellness_decision",
    }.issubset(result["boundaries"])
    assert "context_resolver_does_not_recalculate_caffeine_policy" in result["limitations"]
    assert "private.app.identity" not in json.dumps(result)
