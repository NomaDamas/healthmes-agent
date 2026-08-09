import json
import uuid
from datetime import UTC, date, datetime

import pytest

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityContextResolveRequest,
    ActivityPlatform,
    AppIntervalRecord,
)
from healthmes.activity.resolver import (
    WellnessContextRangeError,
    calendar_context,
    nutrition_context,
    resolve_wellness_context,
)
from healthmes.activity.service import ingest_activity_batch
from healthmes.calendars.base import HealthmesEventKind
from healthmes.store import CalendarEventMirror, CalendarSource


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
    assert result["freshness"]["activity"]["status"] == "retained_raw_window"
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
    assert result["contexts"]["nutrition"]["status"] == "insufficient_data"
    assert result["decision_ready"] is False
    assert result["contexts"]["time"]["local_now"] == "2026-08-01T12:00:00+00:00"
    assert {
        "specialized_policy_numbers_are_not_recomputed",
        "missing_data_is_not_zero",
        "association_is_not_causation",
        "context_only_not_a_final_wellness_decision",
    }.issubset(result["boundaries"])
    assert "context_resolver_does_not_recalculate_caffeine_policy" in result["limitations"]
    assert "private.app.identity" not in json.dumps(result)


async def test_resolver_uses_injected_now_for_an_implicit_local_date(session) -> None:
    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(question_kind="activity_summary"),
        default_timezone="Asia/Seoul",
        now=datetime(2026, 8, 1, 16, tzinfo=UTC),
    )

    assert result["date"] == "2026-08-02"
    assert result["timezone"] == "Asia/Seoul"


async def test_resolver_rejects_a_window_from_another_local_date(session) -> None:
    with pytest.raises(WellnessContextRangeError, match="same local day"):
        await resolve_wellness_context(
            session,
            ActivityContextResolveRequest(
                question_kind="focus",
                date="2026-08-01",
                start=datetime(2026, 8, 2, 9, tzinfo=UTC),
                end=datetime(2026, 8, 2, 10, tzinfo=UTC),
            ),
            default_timezone="UTC",
            now=datetime(2026, 8, 2, 12, tzinfo=UTC),
        )


async def test_wearable_context_from_another_day_is_not_combined(session) -> None:
    _seed_activity(session)

    async def wrong_day(day: date):
        return {
            "status": "ok",
            "date": "2026-08-02",
            "evidence_ids": ["wrong-day"],
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=wrong_day,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["contexts"]["wearable"]["status"] == "insufficient_data"
    assert result["contexts"]["wearable"]["reason"] == "wearable_context_date_mismatch"
    assert result["contexts"]["wearable"]["evidence_ids"] == []
    assert "wrong-day" not in json.dumps(result)


async def test_wearable_context_adds_coverage_freshness_and_evidence_limitation(
    session,
) -> None:
    _seed_activity(session)

    async def sparse_readiness(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "sleep_debt": {
                "status": "ok",
                "recorded_at": "2026-08-01T07:00:00+00:00",
            },
            "hrv": {
                "status": "insufficient_data",
                "recorded_at": "2026-08-01T08:00:00+00:00",
            },
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=sparse_readiness,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    wearable = result["contexts"]["wearable"]

    assert wearable["coverage"] == {
        "status": "readiness_blocks",
        "ratio": 0.5,
        "usable_blocks": 1,
        "total_blocks": 2,
    }
    assert wearable["freshness"] == {
        "recorded_at": "2026-08-01T08:00:00+00:00",
        "status": "derived_from_readiness_blocks",
    }
    assert "wearable_readiness_evidence_ids_unavailable" in wearable["limitations"]


def test_calendar_context_excludes_all_day_and_actual_sleep_rows(session) -> None:
    session.add_all(
        [
            CalendarEventMirror(
                external_id="work",
                calendar_source=CalendarSource.GOOGLE,
                summary="Work",
                start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            ),
            CalendarEventMirror(
                external_id="all-day",
                calendar_source=CalendarSource.GOOGLE,
                summary="Holiday",
                start_at=datetime(2026, 8, 1, tzinfo=UTC),
                end_at=datetime(2026, 8, 2, tzinfo=UTC),
                is_all_day=True,
            ),
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="Sleep",
                start_at=datetime(2026, 8, 1, 1, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 8, tzinfo=UTC),
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
            ),
        ]
    )
    session.flush()

    context = calendar_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )

    assert context["event_count"] == 1
    assert context["busy_minutes"] == 60.0


def _nutrition_snapshot(
    *,
    scope: str = "caffeine_sleep",
    intended_at: str = "2026-08-01T12:00:00+00:00",
    complete: bool = True,
    include_candidate: bool = True,
    candidate_amount: dict | None = None,
    ledger_status: str | None = None,
    ledger_date: str = "2026-08-01",
    ledger_timezone: str = "UTC",
    confirmed_caffeine_mg: object = 0.0,
) -> dict:
    amount = (
        candidate_amount
        if candidate_amount is not None
        else {"kind": "exact", "exact": 100, "unit": "mg"}
    )
    return {
        "request": {
            "scope": scope,
            "requested_at": intended_at,
            "intended_consumption_at": intended_at,
        },
        "candidate": {
            "resolved_items": (
                [
                    {
                        "nutrients": [
                            {
                                "nutrient": "caffeine",
                                "amount": amount,
                            }
                        ]
                    }
                ]
                if include_candidate
                else []
            )
        },
        "specialized_evidence": {
            "caffeine": {
                "status": (
                    ledger_status
                    if ledger_status is not None
                    else "known"
                    if complete
                    else "incomplete"
                ),
                "local_date": ledger_date,
                "timezone": ledger_timezone,
                "confirmed_caffeine_mg": confirmed_caffeine_mg,
                "total_intake_complete": complete,
            }
        },
        "boundaries": {"caffeine_total_intake_complete": complete},
        "evidence_event_ids": ["nutrition-evidence"],
        "limitations": [],
    }


def test_caffeine_nutrition_context_is_ready_only_with_complete_matching_evidence(
    session,
    monkeypatch,
) -> None:
    import healthmes.activity.resolver as resolver_module

    monkeypatch.setattr(
        resolver_module,
        "nutrition_decision_context",
        lambda session, request_id: _nutrition_snapshot(),
    )

    result = nutrition_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        request_id=uuid.uuid4(),
    )

    assert result["status"] == "ok"
    assert result["decision_ready"] is True
    assert result["evidence_ids"] == ["nutrition-evidence"]


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    (
        (
            _nutrition_snapshot(scope="general_wellness"),
            "nutrition_request_scope_is_not_caffeine_sleep",
        ),
        (
            _nutrition_snapshot(intended_at="2026-08-02T12:00:00+00:00"),
            "nutrition_request_date_mismatch",
        ),
        (
            _nutrition_snapshot(complete=False),
            "caffeine_day_not_confirmed_complete",
        ),
        (
            _nutrition_snapshot(ledger_status="incomplete"),
            "caffeine_ledger_status_not_known",
        ),
        (
            _nutrition_snapshot(ledger_date="2026-07-31"),
            "caffeine_ledger_date_mismatch",
        ),
        (
            _nutrition_snapshot(ledger_timezone="Asia/Seoul"),
            "caffeine_ledger_timezone_mismatch",
        ),
        (
            _nutrition_snapshot(confirmed_caffeine_mg=None),
            "caffeine_ledger_amount_missing",
        ),
        (
            _nutrition_snapshot(confirmed_caffeine_mg=float("nan")),
            "caffeine_ledger_amount_missing",
        ),
        (
            _nutrition_snapshot(include_candidate=False),
            "candidate_caffeine_estimate_missing",
        ),
        (
            _nutrition_snapshot(
                candidate_amount={"kind": "unknown", "unit": "mg"}
            ),
            "candidate_caffeine_estimate_missing",
        ),
        (
            _nutrition_snapshot(
                candidate_amount={
                    "kind": "exact",
                    "exact": 100,
                    "unit": "g",
                }
            ),
            "candidate_caffeine_estimate_missing",
        ),
        (
            _nutrition_snapshot(
                candidate_amount={"kind": "exact", "unit": "mg"}
            ),
            "candidate_caffeine_estimate_missing",
        ),
    ),
)
def test_caffeine_nutrition_context_fails_closed(
    session,
    monkeypatch,
    snapshot: dict,
    reason: str,
) -> None:
    import healthmes.activity.resolver as resolver_module

    monkeypatch.setattr(
        resolver_module,
        "nutrition_decision_context",
        lambda session, request_id: snapshot,
    )

    result = nutrition_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        request_id=uuid.uuid4(),
    )

    assert result["status"] == "insufficient_data"
    assert result["decision_ready"] is False
    assert result["reason"] == reason


async def test_wearable_context_recovers_nested_timestamp_from_unavailable_freshness(
    session,
) -> None:
    _seed_activity(session)

    async def nested_readiness(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "actual_sleep": {
                "status": "ok",
                "last_night": {
                    "recorded_at": "2026-08-01T08:00:00+00:00",
                },
            },
            "freshness": {
                "recorded_at": None,
                "status": "unavailable",
            },
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=nested_readiness,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["contexts"]["wearable"]["freshness"] == {
        "recorded_at": "2026-08-01T08:00:00+00:00",
        "status": "derived_from_readiness_blocks",
    }
