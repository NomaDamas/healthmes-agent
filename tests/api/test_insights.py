"""Tests for the insights router: list + deterministic recompute.

The recompute test seeds two days of data (Mon 2026-07-06, Tue 2026-07-07)
and asserts against fully hand-computed aggregates:

Stress samples (garmin_stress_level):
  Mon 09:00=60 09:30=70 | 14:00=30 14:30=40 | 20:00=20 20:15=30
  Tue 09:10=80 09:40=90 | 14:05=20 14:20=30 | 20:05=40 20:20=10

  by hour     9: (60+70+80+90)/4 = 75    14: 30    20: 25
  overall     520/12 = 43.33
  by weekday  mon 250/6 = 41.67   tue 270/6 = 45.0
  'meeting' events cover Mon+Tue 09:00-10:00 -> samples 60,70,80,90 -> mean 75,
  delta 75 - 43.33 = 31.67
  running workouts 16:00-18:30 both days: before-window [14:00,16:00) means
  35 / 25, after-window [18:30,20:30) means 25 / 25 -> deltas -10, 0 -> -5.0
  confidences: hour/weekday 2 days / 14 = 0.143, keyword 4 samples / 30 =
  0.133, activity 2 workouts / 10 = 0.2

The Phase-2 focus template is fed from the local store; its hand-computed
seed lives in the ``seeded_focus_data`` fixture (see its docstring).
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from healthmes.store import (
    AppUsageSample,
    CalendarEventMirror,
    CalendarSource,
    CognitiveEnergyEstimate,
    Insight,
)

USER_ID = "u-1"
PERIOD_BODY = {"period_start": "2026-07-06", "period_end": "2026-07-07"}
PERIOD = "2026-07-06..2026-07-07"

STRESS_POINTS = [
    ("2026-07-06T09:00:00Z", 60),
    ("2026-07-06T09:30:00Z", 70),
    ("2026-07-06T14:00:00Z", 30),
    ("2026-07-06T14:30:00Z", 40),
    ("2026-07-06T20:00:00Z", 20),
    ("2026-07-06T20:15:00Z", 30),
    ("2026-07-07T09:10:00Z", 80),
    ("2026-07-07T09:40:00Z", 90),
    ("2026-07-07T14:05:00Z", 20),
    ("2026-07-07T14:20:00Z", 30),
    ("2026-07-07T20:05:00Z", 40),
    ("2026-07-07T20:20:00Z", 10),
]

WORKOUTS = [
    {
        "id": "w1",
        "type": "running",
        "start_time": "2026-07-06T16:00:00Z",
        "end_time": "2026-07-06T18:30:00Z",
    },
    {
        "id": "w2",
        "type": "running",
        "start_time": "2026-07-07T16:00:00Z",
        "end_time": "2026-07-07T18:30:00Z",
    },
    # Excluded: no stress samples in its [04:00, 06:00) before-window.
    {
        "id": "w3",
        "type": "cycling",
        "start_time": "2026-07-07T06:00:00Z",
        "end_time": "2026-07-07T07:00:00Z",
    },
]


def _stress_page(points):
    return [
        {"timestamp": ts, "type": "garmin_stress_level", "value": value, "unit": "score"}
        for ts, value in points
    ]


def _page(data, next_cursor=None):
    return {
        "data": data,
        "pagination": {"next_cursor": next_cursor, "has_more": next_cursor is not None},
        "metadata": {},
    }


def make_handler(calls, stress_points=STRESS_POINTS, workouts=WORKOUTS, users=None):
    """MockTransport handler serving the vendor REST shapes (2-page timeseries)."""
    if users is None:
        users = [{"id": USER_ID}]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/v1/users":
            return httpx.Response(
                200, json={"items": users, "total": len(users), "page": 1, "limit": 1}
            )
        if path == f"/api/v1/users/{USER_ID}/timeseries":
            cursor = request.url.params.get("cursor")
            first, second = stress_points[:7], stress_points[7:]
            if cursor is None:
                next_cursor = "cursor-2" if second else None
                return httpx.Response(200, json=_page(_stress_page(first), next_cursor))
            assert cursor == "cursor-2"
            return httpx.Response(200, json=_page(_stress_page(second)))
        if path == f"/api/v1/users/{USER_ID}/events/workouts":
            return httpx.Response(200, json=_page(workouts))
        raise AssertionError(f"unexpected request path: {path}")

    return handler


@pytest.fixture
def seeded_calendar(session):
    """Three mirrored events; 'meeting' is the only keyword on >= 2 events."""

    def _dt(value: str) -> datetime:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)

    session.add_all(
        [
            CalendarEventMirror(
                external_id="ev-1",
                calendar_source=CalendarSource.GOOGLE,
                summary="Team standup meeting",
                start_at=_dt("2026-07-06T09:00:00"),
                end_at=_dt("2026-07-06T10:00:00"),
            ),
            CalendarEventMirror(
                external_id="ev-2",
                calendar_source=CalendarSource.GOOGLE,
                summary="Planning meeting",
                start_at=_dt("2026-07-07T09:00:00"),
                end_at=_dt("2026-07-07T10:00:00"),
            ),
            CalendarEventMirror(
                external_id="ev-3",
                calendar_source=CalendarSource.CALDAV,
                summary="Focus block",
                start_at=_dt("2026-07-06T14:00:00"),
                end_at=_dt("2026-07-06T15:00:00"),
            ),
        ]
    )
    session.commit()


def test_recompute_computes_all_four_templates(app, client, seeded_calendar, ow_client_factory):
    calls = []
    app.state.ow_client = ow_client_factory(make_handler(calls))

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["period"] == PERIOD
    assert body["ow_user_id"] == USER_ID
    # No cognitive_energy_estimate rows are seeded here, so only the Phase-2
    # focus template is (honestly) skipped.
    assert body["skipped"] == [{"kind": "focus_drop_by_hour", "reason": "insufficient_data"}]

    by_kind = {insight["kind"]: insight for insight in body["insights"]}
    assert list(by_kind) == [
        "stress_by_hour",
        "stress_by_weekday",
        "stress_by_calendar_keyword",
        "activity_type_vs_stress",
    ]

    hour = by_kind["stress_by_hour"]
    assert hour["statement"] == "Stress peaks around 09:00 UTC (avg 75 vs overall 43)."
    assert hour["confidence"] == 0.143
    assert hour["evidence"]["by_hour"] == {
        "9": {"mean": 75.0, "n": 4},
        "14": {"mean": 30.0, "n": 4},
        "20": {"mean": 25.0, "n": 4},
    }
    assert hour["evidence"]["overall_mean"] == 43.33
    assert hour["evidence"]["n_samples"] == 12
    assert hour["evidence"]["peak_hour"] == 9
    assert hour["evidence"]["low_hour"] == 20

    weekday = by_kind["stress_by_weekday"]
    assert weekday["statement"] == "Stress is highest on Tuesday (avg 45 vs overall 43)."
    assert weekday["confidence"] == 0.143
    assert weekday["evidence"]["by_weekday"] == {
        "mon": {"mean": 41.67, "n": 6},
        "tue": {"mean": 45.0, "n": 6},
    }
    assert weekday["evidence"]["peak_weekday"] == "tue"

    keyword = by_kind["stress_by_calendar_keyword"]
    assert keyword["statement"] == (
        "Calendar events mentioning 'meeting' coincide with higher stress "
        "(75 vs baseline 43, 2 events)."
    )
    assert keyword["confidence"] == 0.133
    assert keyword["evidence"]["baseline_mean"] == 43.33
    assert keyword["evidence"]["top_keyword"] == "meeting"
    assert keyword["evidence"]["keywords"] == [
        {"keyword": "meeting", "n_events": 2, "n_samples": 4, "mean": 75.0, "delta": 31.67}
    ]

    activity = by_kind["activity_type_vs_stress"]
    assert activity["statement"] == (
        "'running' workouts are followed by lower stress "
        "(avg change -5.0 within 2h of finishing, n=2)."
    )
    assert activity["confidence"] == 0.2
    assert activity["evidence"]["types"] == [
        {"type": "running", "n": 2, "mean_before": 30.0, "mean_after": 25.0, "mean_delta": -5.0}
    ]
    assert activity["evidence"]["top_type"] == "running"
    assert activity["evidence"]["n_workouts_total"] == 3
    assert activity["evidence"]["window_hours"] == 2.0

    # Rows were persisted.
    stored = client.get("/v1/insights", params={"period": PERIOD}).json()
    assert stored["pagination"]["total_count"] == 4

    # The client resolved the user, paginated the timeseries (2 pages) and
    # fetched workouts over the requested window.
    paths = [call.url.path for call in calls]
    assert paths == [
        "/api/v1/users",
        f"/api/v1/users/{USER_ID}/timeseries",
        f"/api/v1/users/{USER_ID}/timeseries",
        f"/api/v1/users/{USER_ID}/events/workouts",
    ]
    ts_params = calls[1].url.params
    assert ts_params["types"] == "garmin_stress_level"
    assert ts_params["start_time"] == "2026-07-06T00:00:00+00:00"
    assert ts_params["end_time"] == "2026-07-08T00:00:00+00:00"
    assert calls[2].url.params["cursor"] == "cursor-2"
    assert calls[1].headers["X-Open-Wearables-API-Key"] == "test-key"


def test_recompute_is_idempotent_per_period(app, client, seeded_calendar, ow_client_factory):
    app.state.ow_client = ow_client_factory(make_handler([]))

    first = client.post("/v1/insights/recompute", json=PERIOD_BODY)
    second = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert first.status_code == second.status_code == 200
    stored = client.get("/v1/insights", params={"period": PERIOD}).json()
    assert stored["pagination"]["total_count"] == 4
    assert sorted(i["statement"] for i in first.json()["insights"]) == sorted(
        i["statement"] for i in second.json()["insights"]
    )


def test_recompute_without_data_skips_all_and_replaces_stale_rows(
    app, client, seeded_calendar, ow_client_factory
):
    app.state.ow_client = ow_client_factory(make_handler([]))
    assert client.post("/v1/insights/recompute", json=PERIOD_BODY).status_code == 200
    stored = client.get("/v1/insights", params={"period": PERIOD}).json()
    assert stored["pagination"]["total_count"] == 4

    # Same period, but the upstream now has no stress/workout data.
    app.state.ow_client = ow_client_factory(make_handler([], stress_points=[], workouts=[]))
    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["insights"] == []
    assert {item["kind"] for item in body["skipped"]} == {
        "stress_by_hour",
        "stress_by_weekday",
        "stress_by_calendar_keyword",
        "activity_type_vs_stress",
        "focus_drop_by_hour",
    }
    assert all(item["reason"] == "insufficient_data" for item in body["skipped"])
    # Stale rows for the period were replaced (deleted, nothing re-inserted).
    stored = client.get("/v1/insights", params={"period": PERIOD}).json()
    assert stored["pagination"]["total_count"] == 0


def test_recompute_with_explicit_user_skips_user_resolution(app, client, ow_client_factory):
    calls = []
    app.state.ow_client = ow_client_factory(make_handler(calls))

    response = client.post("/v1/insights/recompute", json={**PERIOD_BODY, "ow_user_id": USER_ID})

    assert response.status_code == 200
    assert all(call.url.path != "/api/v1/users" for call in calls)
    assert response.json()["ow_user_id"] == USER_ID


def test_recompute_unresolvable_user_is_422(app, client, ow_client_factory):
    app.state.ow_client = ow_client_factory(make_handler([], users=[]))

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ow_user_unresolved"


def test_recompute_upstream_failure_is_502(app, client, ow_client_factory):
    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    app.state.ow_client = ow_client_factory(failing)

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_recompute_rejects_inverted_period(client):
    response = client.post(
        "/v1/insights/recompute",
        json={"period_start": "2026-07-07", "period_end": "2026-07-06"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_recompute_rejects_period_over_92_days(app, client, ow_client_factory):
    app.state.ow_client = ow_client_factory(make_handler([]))

    response = client.post(
        "/v1/insights/recompute",
        json={"period_start": "2026-01-01", "period_end": "2026-07-07"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_range"


# ---------------------------------------------------------------------------
# Phase-2 focus template (focus_drop_by_hour) — store-fed, hand-computed seed
# ---------------------------------------------------------------------------

FOCUS_KIND = "focus_drop_by_hour"
FOCUS_DAYS = ("2026-07-06", "2026-07-07")  # Mon, Tue
FOCUS_SLEEP_INDEX = (30.0, 26.0)  # per-day engine sleep-debt index (Mon, Tue)
# Per-hour scores (Mon, Tue): ten normal hours at mean 77, a dip at 14-16h.
FOCUS_HOUR_SCORES = {hour: (75, 79) for hour in (8, 9, 10, 11, 12, 13, 16, 17, 18, 19)}
FOCUS_HOUR_SCORES[14] = (52, 48)
FOCUS_HOUR_SCORES[15] = (46, 42)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _components(score: int, sleep_index: float) -> dict:
    """Minimal engine components payload carrying the sleep-debt index."""
    return {
        "version": 1,
        "items": [
            {
                "name": "sleep_debt_penalty",
                "kind": "penalty",
                "weight": 0.3,
                "raw": {"index": sleep_index, "severity": sleep_index / 100.0},
                "contribution": -0.3 * sleep_index,
            }
        ],
        "score_exact": float(score),
    }


def _estimate_row(start: datetime, score: int, sleep_index: float) -> CognitiveEnergyEstimate:
    return CognitiveEnergyEstimate(
        window_start=start,
        window_end=start + timedelta(hours=1),
        score=score,
        components=_components(score, sleep_index),
    )


@pytest.fixture
def seeded_focus_data(session):
    """Hand-computed focus seed over Mon 2026-07-06 + Tue 2026-07-07 (UTC).

    Energy windows (24 qualified + 1 lone Mon 20:00 window of score 70):
      hours 8-13,16-19: Mon 75 / Tue 79 -> mean 77 (x10 hours)
      hour 14: 52/48 -> mean 50   hour 15: 46/42 -> mean 44
      baseline = (10*154 + 100 + 88) / 24 = 1728/24 = 72.0
      deficits: h14 = 22, h15 = 28 (>= 10) -> block 14-16h,
      dip mean = (52+48+46+42)/4 = 47.0, deficit = 25.0
    Sleep-debt index: Mon windows 30.0, Tue 26.0 -> dip mean (30+30+26+26)/4 = 28.0
    App usage (both days covered -> 4 dip windows = 4.0 h):
      Slack in dip windows 5+4+5+3 + 6+4+5+4 = 36 -> 9.0/hour
      Instagram in dip windows 2+2 = 4 -> total 40 -> 10.0/hour
      (Slack 09:00 and 13:30 buckets sit outside the dip block)
    Calendar mornings (06:00-12:00 starts): Mon 3, Tue 2 -> mean 2.5 < 3.0
      (12:00 lunch and 14:00 deep-work block never count as morning)
    Confidence = 2 days / 14 = 0.143.
    """
    rows: list[CognitiveEnergyEstimate] = []
    for day_index, day in enumerate(FOCUS_DAYS):
        for hour, scores in sorted(FOCUS_HOUR_SCORES.items()):
            start = _utc(f"{day}T{hour:02d}:00:00")
            rows.append(_estimate_row(start, scores[day_index], FOCUS_SLEEP_INDEX[day_index]))
    # Lone hour-20 window: n=1 -> excluded from the per-hour profile/baseline.
    rows.append(_estimate_row(_utc("2026-07-06T20:00:00"), 70, FOCUS_SLEEP_INDEX[0]))
    session.add_all(rows)

    usage_points = [
        ("2026-07-06T09:00:00", "com.Slack", 7, "communication"),  # outside dip
        ("2026-07-06T13:30:00", "com.Slack", 5, "communication"),  # outside dip
        ("2026-07-06T14:00:00", "com.Slack", 5, "communication"),
        ("2026-07-06T14:30:00", "com.Slack", 4, "communication"),
        ("2026-07-06T14:00:00", "com.instagram.android", 2, "social"),
        ("2026-07-06T15:00:00", "com.Slack", 5, "communication"),
        ("2026-07-06T15:30:00", "com.Slack", 3, "communication"),
        ("2026-07-07T14:00:00", "com.Slack", 6, "communication"),
        ("2026-07-07T14:30:00", "com.Slack", 4, "communication"),
        ("2026-07-07T15:00:00", "com.Slack", 5, "communication"),
        ("2026-07-07T15:30:00", "com.Slack", 4, "communication"),
        ("2026-07-07T15:00:00", "com.instagram.android", 2, "social"),
    ]
    session.add_all(
        [
            AppUsageSample(
                device_id="pixel-1",
                bucket_start=_utc(ts),
                app_package=package,
                foreground_seconds=300,
                launches=launches,
                category=category,
            )
            for ts, package, launches, category in usage_points
        ]
    )

    calendar_events = [
        ("f-1", "Standup", "2026-07-06T08:00:00", "2026-07-06T08:30:00"),
        ("f-2", "1:1 sync", "2026-07-06T09:30:00", "2026-07-06T10:00:00"),
        ("f-3", "Design review", "2026-07-06T11:00:00", "2026-07-06T11:45:00"),
        ("f-4", "Planning", "2026-07-07T09:00:00", "2026-07-07T10:00:00"),
        ("f-5", "Retro", "2026-07-07T10:00:00", "2026-07-07T11:00:00"),
        ("f-6", "Lunch", "2026-07-07T12:00:00", "2026-07-07T13:00:00"),
        ("f-7", "Deep work", "2026-07-06T14:00:00", "2026-07-06T15:00:00"),
    ]
    session.add_all(
        [
            CalendarEventMirror(
                external_id=external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary=summary,
                start_at=_utc(start),
                end_at=_utc(end),
            )
            for external_id, summary, start, end in calendar_events
        ]
    )
    session.commit()


def test_recompute_emits_focus_insight_with_handcomputed_evidence(
    app, client, seeded_focus_data, ow_client_factory
):
    app.state.ow_client = ow_client_factory(make_handler([], stress_points=[], workouts=[]))

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 200, response.text
    body = response.json()
    # No stress data upstream -> the four Phase-1 templates skip; only the
    # store-fed focus template computes.
    assert {item["kind"] for item in body["skipped"]} == {
        "stress_by_hour",
        "stress_by_weekday",
        "stress_by_calendar_keyword",
        "activity_type_vs_stress",
    }
    assert len(body["insights"]) == 1
    focus = body["insights"][0]
    assert focus["kind"] == FOCUS_KIND
    assert focus["period"] == PERIOD
    assert focus["statement"] == (
        "14-16h focus drop (energy 47 vs baseline 72, UTC): sleep deficit + Slack 9 launches/hour."
    )
    assert focus["confidence"] == 0.143

    evidence = focus["evidence"]
    assert evidence["baseline_mean"] == 72.0
    assert evidence["dip_threshold_points"] == 10.0
    assert evidence["n_windows"] == 24
    assert evidence["n_windows_total"] == 25
    assert evidence["n_days"] == 2
    expected_by_hour = {
        str(hour): {"mean": 77.0, "n": 2} for hour in (8, 9, 10, 11, 12, 13, 16, 17, 18, 19)
    }
    expected_by_hour["14"] = {"mean": 50.0, "n": 2}
    expected_by_hour["15"] = {"mean": 44.0, "n": 2}
    assert evidence["by_hour"] == expected_by_hour
    assert evidence["unqualified_hours"] == {"20": 1}
    assert evidence["dip_hours"] == [14, 15]
    assert evidence["block"] == {
        "label": "14-16h",
        "start_hour": 14,
        "end_hour": 16,
        "hours": [14, 15],
        "n_windows": 4,
        "mean": 47.0,
        "deficit": 25.0,
        "days": ["2026-07-06", "2026-07-07"],
    }
    assert evidence["factors"]["sleep_deficit"] == {
        "qualified": True,
        "mean_debt_index": 28.0,
        "n_windows": 4,
        "threshold": 25.0,
    }
    assert evidence["factors"]["app_switching"] == {
        "qualified": True,
        "app_package": "com.Slack",
        "app_label": "Slack",
        "launches": 36,
        "launches_per_hour": 9.0,
        "total_launches": 40,
        "total_launches_per_hour": 10.0,
        "windows_covered": 4,
        "hours_covered": 4.0,
        "threshold": 6.0,
    }
    assert evidence["factors"]["meeting_dense_mornings"] == {
        "qualified": False,
        "mean_morning_events": 2.5,
        "n_days": 2,
        "morning_hours": "06:00-12:00",
        "threshold": 3.0,
    }

    # Persisted and re-listable through the filter API.
    stored = client.get("/v1/insights", params={"period": PERIOD, "kind": FOCUS_KIND}).json()
    assert stored["pagination"]["total_count"] == 1


def test_recompute_focus_is_idempotent_per_period(
    app, client, seeded_focus_data, ow_client_factory
):
    app.state.ow_client = ow_client_factory(make_handler([], stress_points=[], workouts=[]))

    first = client.post("/v1/insights/recompute", json=PERIOD_BODY)
    second = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert first.status_code == second.status_code == 200
    assert first.json()["insights"][0]["statement"] == second.json()["insights"][0]["statement"]
    stored = client.get("/v1/insights", params={"period": PERIOD, "kind": FOCUS_KIND}).json()
    assert stored["pagination"]["total_count"] == 1


def test_recompute_focus_sparse_data_produces_no_insight(app, client, session, ow_client_factory):
    """Six windows on a single day never yield a focus insight — even with
    heavy app switching and meeting-dense mornings (no false positives)."""
    app.state.ow_client = ow_client_factory(make_handler([], stress_points=[], workouts=[]))
    for hour, score in ((10, 80), (11, 78), (12, 55), (13, 50), (14, 77), (15, 79)):
        start = _utc(f"2026-07-06T{hour:02d}:00:00")
        session.add(
            CognitiveEnergyEstimate(
                window_start=start,
                window_end=start + timedelta(hours=1),
                score=score,
                components={"version": 1, "items": [], "score_exact": float(score)},
            )
        )
    session.add(
        AppUsageSample(
            device_id="pixel-1",
            bucket_start=_utc("2026-07-06T12:00:00"),
            app_package="com.Slack",
            foreground_seconds=1800,
            launches=30,
            category="communication",
        )
    )
    session.add_all(
        [
            CalendarEventMirror(
                external_id=f"m-{index}",
                calendar_source=CalendarSource.GOOGLE,
                summary="Morning meeting",
                start_at=_utc(f"2026-07-06T{6 + index:02d}:00:00"),
                end_at=_utc(f"2026-07-06T{6 + index:02d}:30:00"),
            )
            for index in range(3)
        ]
    )
    session.commit()

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["insights"] == []
    skipped = {item["kind"]: item["reason"] for item in body["skipped"]}
    assert skipped[FOCUS_KIND] == "insufficient_data"
    stored = client.get("/v1/insights", params={"period": PERIOD}).json()
    assert stored["pagination"]["total_count"] == 0


def test_recompute_focus_flat_profile_reports_no_dip(app, client, session, ow_client_factory):
    """A dense but flat energy profile is honestly 'no_dip_detected' — heavy
    co-occurring factors alone can never fabricate a focus drop."""
    app.state.ow_client = ow_client_factory(make_handler([], stress_points=[], workouts=[]))
    for day in FOCUS_DAYS:
        for hour in range(8, 20):
            start = _utc(f"{day}T{hour:02d}:00:00")
            session.add(
                CognitiveEnergyEstimate(
                    window_start=start,
                    window_end=start + timedelta(hours=1),
                    score=75,
                    components=_components(75, 40.0),
                )
            )
        session.add(
            AppUsageSample(
                device_id="pixel-1",
                bucket_start=_utc(f"{day}T14:00:00"),
                app_package="com.Slack",
                foreground_seconds=1800,
                launches=30,
                category="communication",
            )
        )
    session.commit()

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["insights"] == []
    skipped = {item["kind"]: item["reason"] for item in body["skipped"]}
    assert skipped[FOCUS_KIND] == "no_dip_detected"


# ---------------------------------------------------------------------------
# User-timezone bucketing (Settings.timezone) + truncation honesty
# ---------------------------------------------------------------------------

# 16 samples inside 2026-07-06..07 (UTC), hand-computed for Asia/Seoul (+9):
#   6x 07-06T13:0x Z = 22:00 KST Mon, value 80
#   6x 07-06T22:0x Z = 07:00 KST TUE (weekday shifts across midnight), value 20
#   4x 07-07T05:0x Z = 14:00 KST Tue, value 40
# KST hours {22: mean 80 n6, 7: mean 20 n6, 14: mean 40 n4}; overall 47.5.
# UTC bucketing would instead put the peak at 13:00 — the exact mislabeling
# the fix removes.
SEOUL_STRESS_POINTS = (
    [(f"2026-07-06T13:0{i}:00Z", 80) for i in range(6)]
    + [(f"2026-07-06T22:0{i}:00Z", 20) for i in range(6)]
    + [(f"2026-07-07T05:0{i}:00Z", 40) for i in range(4)]
)


def test_recompute_buckets_hours_and_weekdays_in_user_timezone(
    app, client, ow_client_factory, settings
):
    app.state.settings = settings.model_copy(update={"timezone": "Asia/Seoul"})
    app.state.ow_client = ow_client_factory(
        make_handler([], stress_points=SEOUL_STRESS_POINTS, workouts=[])
    )

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 200, response.text
    by_kind = {insight["kind"]: insight for insight in response.json()["insights"]}

    hour = by_kind["stress_by_hour"]
    assert hour["statement"] == (
        "Stress peaks around 22:00 Asia/Seoul (avg 80 vs overall 48)."
    )
    assert set(hour["evidence"]["by_hour"]) == {"7", "14", "22"}
    assert hour["evidence"]["by_hour"]["22"] == {"mean": 80.0, "n": 6}
    assert hour["evidence"]["timezone"] == "Asia/Seoul"

    weekday = by_kind["stress_by_weekday"]
    # The 07-06T22:0xZ samples belong to Tuesday in KST (07:00 next day).
    assert weekday["evidence"]["by_weekday"] == {
        "mon": {"mean": 80.0, "n": 6},
        "tue": {"mean": 28.0, "n": 10},
    }
    assert weekday["statement"].startswith("Stress is highest on Monday")


def test_recompute_refuses_truncated_timeseries(app, client, ow_client_factory):
    """A fetch that exhausts the page budget with data remaining must fail
    loudly, never compute a confidently wrong insight over a partial window
    (the old fixed 10k cap silently kept ~7 days of a 28-day window)."""

    def endless_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/users":
            return httpx.Response(
                200, json={"items": [{"id": USER_ID}], "total": 1, "page": 1, "limit": 2}
            )
        if path == f"/api/v1/users/{USER_ID}/timeseries":
            cursor = int(request.url.params.get("cursor") or "0")
            return httpx.Response(
                200,
                json=_page(
                    _stress_page([(f"2026-07-06T09:{cursor % 60:02d}:00Z", 50)]),
                    next_cursor=str(cursor + 1),  # never ends
                ),
            )
        raise AssertionError(f"unexpected request path: {path}")

    app.state.ow_client = ow_client_factory(endless_handler)

    response = client.post("/v1/insights/recompute", json=PERIOD_BODY)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_truncated"


def test_list_insights_filters_and_paginates(client, session):
    session.add_all(
        [
            Insight(period="2026-06-01..2026-06-28", kind="stress_by_hour", statement="a"),
            Insight(period=PERIOD, kind="stress_by_hour", statement="b"),
            Insight(period=PERIOD, kind="stress_by_weekday", statement="c"),
        ]
    )
    session.commit()

    by_period = client.get("/v1/insights", params={"period": PERIOD}).json()
    assert by_period["pagination"]["total_count"] == 2

    by_kind = client.get(
        "/v1/insights", params={"period": PERIOD, "kind": "stress_by_weekday"}
    ).json()
    assert [i["statement"] for i in by_kind["data"]] == ["c"]

    paged = client.get("/v1/insights", params={"limit": 1}).json()
    assert paged["pagination"]["total_count"] == 3
    assert paged["pagination"]["has_more"] is True
    assert len(paged["data"]) == 1


def test_list_insights_rejects_limit_above_200(client):
    response = client.get("/v1/insights", params={"limit": 500})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
