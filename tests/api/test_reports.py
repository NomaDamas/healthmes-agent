"""Weekly report page tests (docs/PLAN.md §8.5 "Mermaid tree + weekly report").

Covers, over one seeded week (frozen at 2026-07-10, UTC settings):

- ``GET /reports/weekly.json`` — the computed numbers of every section
  (energy per-day aggregates, insight rows + confidence badge levels,
  proposal status counts, alert digest vs budget, decision links);
- ``GET /reports/weekly`` — the rendered HTML carries exactly those numbers
  (parity is asserted against the JSON twin, not re-derived);
- the empty-week case (nulls/zeros, honest "no data" copy, still 200);
- viewer auth: bearer plus the derived read-only ``?token=`` credential
  (the same construction as decision links);
- pure helpers: :func:`weekly_report_url`, :func:`build_energy_sparkline`,
  :func:`confidence_level`;
- the bootstrap Sunday briefing prompt mentions the report link.
"""

import importlib.util
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.api.briefing import decision_viewer_url
from healthmes.api.reports import (
    EnergyDayOut,
    build_energy_sparkline,
    confidence_level,
    weekly_report_url,
)
from healthmes.app import create_app
from healthmes.store import (
    Base,
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    Insight,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TriggerEvent,
)
from healthmes.store.session import get_engine

REPO_ROOT = Path(__file__).resolve().parents[2]

# Local "today" is 2026-07-10 (Friday) => report window 2026-07-04..2026-07-10
# (settings fixture pins timezone=UTC, so local days == UTC days).
FROZEN_NOW = "2026-07-10 12:00:00"
TOKEN = "reports-test-token-123"

TREE = {"id": "root", "type": "rule", "label": "seeded", "children": []}


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _prime_routes(test_client: TestClient) -> None:
    """Build the lazy route schemas outside any freezegun window.

    FastAPI (>= 0.139 lazy routing) builds every route's model fields on the
    first matching request; freezegun's patched ``datetime.date`` breaks
    pydantic schema generation for date-typed query params (goals router), so
    the fixture issues one unfrozen request before the frozen test bodies
    (same warm-up as tests/api/test_briefing.py).
    """
    assert test_client.get("/reports/weekly.json").status_code == 200


@pytest.fixture
def client(app):
    """The shared api-test app (reports router included), lifespan running."""
    with TestClient(app) as test_client:
        _prime_routes(test_client)
        yield test_client


def _seed_week(session) -> dict:
    """One deterministic week of data; returns the rows assertions refer to."""
    # Energy: 2026-07-08 avg 65 (60/70), 2026-07-04 avg 50; 2026-07-03 outside.
    session.add_all(
        [
            CognitiveEnergyEstimate(
                window_start=_utc(2026, 7, 8, 9),
                window_end=_utc(2026, 7, 8, 10),
                score=60,
                components={},
            ),
            CognitiveEnergyEstimate(
                window_start=_utc(2026, 7, 8, 10),
                window_end=_utc(2026, 7, 8, 11),
                score=70,
                components={},
            ),
            CognitiveEnergyEstimate(
                window_start=_utc(2026, 7, 4, 8),
                window_end=_utc(2026, 7, 4, 9),
                score=50,
                components={},
            ),
            CognitiveEnergyEstimate(
                window_start=_utc(2026, 7, 3, 23),
                window_end=_utc(2026, 7, 4, 0),
                score=99,
                components={},
            ),
        ]
    )

    # Insights: three inside the window (high / none / low), one outside.
    insights = [
        Insight(
            period="2026-06-13..2026-07-10",
            kind="stress_by_hour",
            statement="Stress peaks around 14:00 on workdays",
            confidence=0.9,
        ),
        Insight(
            period="2026-06-13..2026-07-10",
            kind="manual_note",
            statement="Hydration correlates with calmer afternoons",
            confidence=None,
        ),
        Insight(
            period="2026-06-13..2026-07-10",
            kind="focus_drop_by_hour",
            statement="Focus dips at 15:00 after short sleep",
            confidence=0.2,
        ),
        Insight(
            period="2026-05-01..2026-05-28",
            kind="stress_by_weekday",
            statement="OLD insight outside the report window",
            confidence=0.8,
        ),
    ]
    for insight, created in zip(
        insights,
        (_utc(2026, 7, 9, 10), _utc(2026, 7, 6, 9), _utc(2026, 7, 5, 9), _utc(2026, 6, 30, 9)),
        strict=True,
    ):
        insight.created_at = created
    session.add_all(insights)

    # Proposals: one per status inside the window, one accepted outside.
    task = Task(title="Weekly report seed task")
    session.add(task)
    session.flush()
    statuses_and_created = [
        (ProposalStatus.ACCEPTED, _utc(2026, 7, 7, 9)),
        (ProposalStatus.PUSHED, _utc(2026, 7, 8, 9)),
        (ProposalStatus.DECLINED, _utc(2026, 7, 9, 9)),
        (ProposalStatus.PROPOSED, _utc(2026, 7, 10, 9)),
        (ProposalStatus.ACCEPTED, _utc(2026, 6, 28, 9)),  # outside
    ]
    for status, created in statuses_and_created:
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=_utc(2026, 7, 11, 10),
            proposed_end=_utc(2026, 7, 11, 11),
            status=status,
        )
        proposal.created_at = created
        session.add(proposal)

    # Trigger events: 3 fired inside (2 delivered), 1 outside the window.
    session.add_all(
        [
            TriggerEvent(
                fired_at=_utc(2026, 7, 8, 9), rule_id="stress_spike", alert_sent=True
            ),
            TriggerEvent(
                fired_at=_utc(2026, 7, 9, 9), rule_id="stress_spike", alert_sent=False
            ),
            TriggerEvent(
                fired_at=_utc(2026, 7, 10, 8), rule_id="deadline_risk", alert_sent=True
            ),
            TriggerEvent(
                fired_at=_utc(2026, 7, 1, 9), rule_id="stress_spike", alert_sent=True
            ),
        ]
    )

    # Decisions: two inside (alert + schedule_change), one outside.
    decision_alert = DecisionRecord(
        kind=DecisionKind.ALERT, tree=TREE, summary="Alert: moved afternoon block"
    )
    decision_alert.created_at = _utc(2026, 7, 9, 11)
    decision_schedule = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        tree=TREE,
        summary="Planned deep work Tuesday morning",
    )
    decision_schedule.created_at = _utc(2026, 7, 6, 8)
    decision_old = DecisionRecord(
        kind=DecisionKind.CAPTURE, tree=TREE, summary="Old decision outside the window"
    )
    decision_old.created_at = _utc(2026, 6, 25, 8)
    session.add_all([decision_alert, decision_schedule, decision_old])

    session.commit()
    for row in (decision_alert, decision_schedule):
        session.refresh(row)
    return {"decision_alert": decision_alert, "decision_schedule": decision_schedule}


# --- JSON: computed numbers --------------------------------------------------


def test_weekly_report_json_computes_numbers(client, session):
    seeded = _seed_week(session)

    with freeze_time(FROZEN_NOW):
        response = client.get("/reports/weekly.json")

    assert response.status_code == 200
    body = response.json()

    assert body["week_start"] == "2026-07-04"
    assert body["week_end"] == "2026-07-10"
    assert body["timezone"] == "UTC"
    assert body["report_url"] == "http://healthmes.test:8100/reports/weekly"

    # Energy: 7 local days, aggregates only from persisted in-window rows.
    days = {day["date"]: day for day in body["energy"]["days"]}
    assert [day["date"] for day in body["energy"]["days"]] == [
        f"2026-07-{num:02d}" for num in range(4, 11)
    ]
    assert days["2026-07-08"] == {
        "date": "2026-07-08",
        "avg_score": 65,
        "min_score": 60,
        "max_score": 70,
        "samples": 2,
    }
    assert days["2026-07-04"]["avg_score"] == 50
    assert days["2026-07-04"]["samples"] == 1
    assert days["2026-07-05"] == {
        "date": "2026-07-05",
        "avg_score": None,
        "min_score": None,
        "max_score": None,
        "samples": 0,
    }
    assert body["energy"]["overall_avg"] == 60  # round((60+70+50)/3)
    assert body["energy"]["samples"] == 3  # the 2026-07-03 window is excluded

    # Insights: in-window rows only, newest first, badge ladder applied.
    assert body["insights"]["count"] == 3
    statements = [item["statement"] for item in body["insights"]["items"]]
    assert statements == [
        "Stress peaks around 14:00 on workdays",
        "Hydration correlates with calmer afternoons",
        "Focus dips at 15:00 after short sleep",
    ]
    levels = {item["statement"]: item["confidence_level"] for item in body["insights"]["items"]}
    assert levels["Stress peaks around 14:00 on workdays"] == "high"
    assert levels["Hydration correlates with calmer afternoons"] == "none"
    assert levels["Focus dips at 15:00 after short sleep"] == "low"

    # Schedule adherence: one proposal per status created in the window.
    assert body["schedule"] == {
        "proposed": 1,
        "accepted": 1,
        "pushed": 1,
        "declined": 1,
        "decided": 3,
        "acceptance_pct": 67,  # round(100 * 2 / 3)
    }

    # Alert digest: fired vs delivered vs the weekly budget (8/day from settings).
    assert body["alerts"]["fired"] == 3
    assert body["alerts"]["delivered"] == 2
    assert body["alerts"]["daily_budget"] == 8
    assert body["alerts"]["weekly_budget"] == 56
    assert body["alerts"]["by_rule"] == [
        {"rule_id": "stress_spike", "fired": 2, "delivered": 1},
        {"rule_id": "deadline_risk", "fired": 1, "delivered": 1},
    ]

    # Decisions: newest first, zero-filled kind counts, viewer links.
    assert body["decisions"]["count"] == 2
    assert body["decisions"]["kind_counts"] == {
        "schedule_change": 1,
        "alert": 1,
        "insight": 0,
        "capture": 0,
    }
    items = body["decisions"]["items"]
    assert [item["summary"] for item in items] == [
        "Alert: moved afternoon block",
        "Planned deep work Tuesday morning",
    ]
    alert_id = str(seeded["decision_alert"].id)
    assert items[0]["id"] == alert_id
    assert items[0]["url"] == f"http://healthmes.test:8100/decisions/{alert_id}"


# --- HTML: same numbers, rendered --------------------------------------------


def test_weekly_report_html_matches_json(client, session):
    seeded = _seed_week(session)

    with freeze_time(FROZEN_NOW):
        page = client.get("/reports/weekly")
        body = client.get("/reports/weekly.json").json()

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    html = page.text

    # Parity: the id-tagged stats render exactly the JSON numbers.
    assert f'id="energy-overall">{body["energy"]["overall_avg"]}<' in html
    assert f'id="insights-count">{body["insights"]["count"]}<' in html
    assert f'id="schedule-accepted">{body["schedule"]["accepted"]}<' in html
    assert f'id="schedule-pushed">{body["schedule"]["pushed"]}<' in html
    assert f'id="schedule-declined">{body["schedule"]["declined"]}<' in html
    assert f'id="schedule-proposed">{body["schedule"]["proposed"]}<' in html
    assert f'id="schedule-acceptance">{body["schedule"]["acceptance_pct"]}<' in html
    assert f'id="alerts-fired">{body["alerts"]["fired"]}<' in html
    assert f'id="alerts-delivered">{body["alerts"]["delivered"]}<' in html
    assert f'id="alerts-budget">{body["alerts"]["weekly_budget"]}<' in html
    assert f'id="decisions-count">{body["decisions"]["count"]}<' in html

    # Energy table rows carry the per-day aggregates.
    assert "Wed 2026-07-08" in html
    assert '<td class="num">65</td>' in html
    assert '<td class="num">70</td>' in html
    assert "Sat 2026-07-04" in html

    # Inline SVG sparkline, local assets only (dots for the two data days).
    assert 'viewBox="0 0 336 72"' in html
    assert html.count("<circle") == 2
    assert "cdn" not in html.lower()
    assert "<script" not in html  # pure HTML+SVG page, no JS needed

    # Insights with confidence badges (n/a for a null confidence).
    assert "Stress peaks around 14:00 on workdays" in html
    assert "conf-high" in html
    assert "n/a" in html
    assert "OLD insight outside the report window" not in html

    # Alert rule breakdown.
    assert "stress_spike" in html
    assert "deadline_risk" in html

    # Decision links into /decisions/{id}, newest first, window-filtered.
    alert_id = str(seeded["decision_alert"].id)
    schedule_id = str(seeded["decision_schedule"].id)
    assert f'href="http://healthmes.test:8100/decisions/{alert_id}"' in html
    assert f'href="http://healthmes.test:8100/decisions/{schedule_id}"' in html
    assert html.index("Alert: moved afternoon block") < html.index(
        "Planned deep work Tuesday morning"
    )
    assert "Old decision outside the window" not in html

    # Cross-link to the JSON twin.
    assert 'href="http://healthmes.test:8100/reports/weekly.json"' in html


# --- empty week ---------------------------------------------------------------


def test_weekly_report_empty_week(client):
    with freeze_time(FROZEN_NOW):
        page = client.get("/reports/weekly")
        body = client.get("/reports/weekly.json").json()

    assert page.status_code == 200
    html = page.text

    assert len(body["energy"]["days"]) == 7
    assert all(day["samples"] == 0 and day["avg_score"] is None for day in body["energy"]["days"])
    assert body["energy"]["overall_avg"] is None
    assert body["energy"]["samples"] == 0
    assert body["insights"] == {"count": 0, "items": []}
    assert body["schedule"] == {
        "proposed": 0,
        "accepted": 0,
        "pushed": 0,
        "declined": 0,
        "decided": 0,
        "acceptance_pct": None,
    }
    assert body["alerts"]["fired"] == 0
    assert body["alerts"]["delivered"] == 0
    assert body["alerts"]["weekly_budget"] == 56  # the budget is still shown
    assert body["alerts"]["by_rule"] == []
    assert body["decisions"]["count"] == 0
    assert body["decisions"]["items"] == []
    assert body["decisions"]["kind_counts"] == {
        "schedule_change": 0,
        "alert": 0,
        "insight": 0,
        "capture": 0,
    }

    assert "No energy data recorded this week." in html
    assert 'id="insights-count">0<' in html
    assert "No proposals were decided this week." in html
    assert 'id="alerts-fired">0<' in html
    assert "No decisions recorded this week." in html


# --- auth: bearer everywhere, derived ?token= for the viewer pages ------------


@contextmanager
def _secured_client(settings):
    """Real app factory with an API token; schema created on the lifespan engine."""
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    application = create_app(secured)
    with TestClient(application) as test_client:
        Base.metadata.create_all(get_engine())
        yield test_client


def test_report_pages_accept_the_derived_viewer_token(settings):
    with _secured_client(settings) as client:
        assert client.get("/reports/weekly").status_code == 401
        assert client.get("/reports/weekly", params={"token": "nope"}).status_code == 401

        page = client.get("/reports/weekly", params={"token": viewer_token(TOKEN)})
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        # The page's own links carry the derived credential, never the API token.
        assert f"?token={viewer_token(TOKEN)}" in page.text
        assert TOKEN not in page.text

        json_via_viewer = client.get(
            "/reports/weekly.json", params={"token": viewer_token(TOKEN)}
        )
        assert json_via_viewer.status_code == 200

        json_via_bearer = client.get(
            "/reports/weekly.json", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert json_via_bearer.status_code == 200
        assert json_via_bearer.json()["report_url"] == (
            f"http://healthmes.test:8100/reports/weekly?token={viewer_token(TOKEN)}"
        )


# --- pure helpers --------------------------------------------------------------


def test_weekly_report_url_matches_decision_link_construction(settings):
    assert weekly_report_url(settings) == "http://healthmes.test:8100/reports/weekly"

    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    url = weekly_report_url(secured)
    assert url == f"http://healthmes.test:8100/reports/weekly?token={viewer_token(TOKEN)}"
    # Same derived credential as decision viewer links (reuse, not re-derivation).
    decision_url = decision_viewer_url(secured, "abc")
    assert url.rsplit("?token=", 1)[1] == decision_url.rsplit("?token=", 1)[1]


def test_build_energy_sparkline_segments_and_gaps():
    scores = [60, None, 40, 50, None, None, 80]
    days = [
        EnergyDayOut(
            date=date(2026, 7, 4 + offset),
            avg_score=score,
            min_score=score,
            max_score=score,
            samples=0 if score is None else 1,
        )
        for offset, score in enumerate(scores)
    ]

    view = build_energy_sparkline(days)

    assert view.width == 336
    assert view.height == 72
    # Only the consecutive run (indexes 2-3) forms a polyline; isolated days
    # (indexes 0 and 6) stay dots — gaps are never interpolated across.
    assert view.segments == ["120,42.0 168,36.0"]
    assert [(point.x, point.y) for point in view.points] == [
        (24, 30.0),
        (120, 42.0),
        (168, 36.0),
        (312, 18.0),
    ]
    assert view.points[0].label == "2026-07-04: avg 60"
    assert view.gridlines == [66.0, 36.0, 6.0]  # scores 0 / 50 / 100

    empty = build_energy_sparkline(
        [
            EnergyDayOut(
                date=date(2026, 7, 4), avg_score=None, min_score=None, max_score=None, samples=0
            )
        ]
    )
    assert empty.segments == []
    assert empty.points == []


def test_confidence_level_ladder():
    assert confidence_level(None) == "none"
    assert confidence_level(0.0) == "low"
    assert confidence_level(0.39) == "low"
    assert confidence_level(0.4) == "medium"
    assert confidence_level(0.74) == "medium"
    assert confidence_level(0.75) == "high"
    assert confidence_level(1.0) == "high"


# --- bootstrap: the Sunday briefing links the report ---------------------------


def test_bootstrap_weekly_prompt_mentions_the_report_link():
    """Only the Sunday weekly-planning prompt carries the report link — and it
    instructs verbatim use of the snapshot's server-built ``weekly_report.url``
    (token embedded by :func:`weekly_report_url` server-side). The agent must
    never hand-construct viewer links: a bare public-base-URL + /reports/weekly
    401s on every token-gated (i.e. phone-tappable) deployment.
    """
    spec = importlib.util.spec_from_file_location(
        "healthmes_bootstrap_reports_test", REPO_ROOT / "scripts" / "bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec: the @dataclass decorator resolves the module via
    # sys.modules (same recipe as tests/glue/conftest.py).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    jobs = {job["name"]: job for job in module.BRIEFING_JOBS}
    weekly = jobs["healthmes-weekly-plan"]["prompt"]
    assert "weekly_report.url" in weekly  # the snapshot field, used verbatim
    assert "verbatim" in weekly
    assert "Never construct" in weekly  # hand-built URLs are forbidden
    for other in ("healthmes-morning-plan", "healthmes-evening-review"):
        assert "/reports/weekly" not in jobs[other]["prompt"]
        assert "weekly_report.url" not in jobs[other]["prompt"]
