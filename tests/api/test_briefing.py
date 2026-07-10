"""Tests for GET /v1/briefing/glance (issue #7 server-side contract).

The briefing router is wired into ``healthmes.api.include_all``, so the
shared app fixture already mounts it. Time is controlled with freezegun;
every expected value below is hand-computed from the seeds.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.app import create_app
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TriggerEvent,
)
from healthmes.store.session import get_engine, get_session, get_session_factory

BASE_URL = "http://healthmes.test:8100"  # settings.public_base_url (tests/conftest.py)
FROZEN_NOW = "2026-07-09 14:23:00"
GLANCE = "/v1/briefing/glance"

# Deterministic ids for exact-payload assertions.
TASK_DEEP_ID = uuid.UUID("00000000-0000-0000-0000-00000000a001")
TASK_REPORT_ID = uuid.UUID("00000000-0000-0000-0000-00000000a002")
DECISION_ALERT_OLD_ID = uuid.UUID("00000000-0000-0000-0000-00000000d001")
DECISION_ALERT_TOP_ID = uuid.UUID("00000000-0000-0000-0000-00000000d002")
DECISION_SCHEDULE_ID = uuid.UUID("00000000-0000-0000-0000-00000000d003")


def _utc(day: int, hour: int, minute: int = 0, month: int = 7) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def _estimate(start: datetime, score: int) -> CognitiveEnergyEstimate:
    return CognitiveEnergyEstimate(
        window_start=start,
        window_end=start + timedelta(hours=1),
        score=score,
        components={"version": 1, "items": [], "score_exact": float(score)},
    )


def _prime_routes(test_client: TestClient) -> None:
    """Build the lazy route schemas outside any freezegun window.

    FastAPI (>= 0.139 lazy routing) builds every route's model fields on the
    first matching request; freezegun's patched ``datetime.date`` breaks
    pydantic schema generation for date-typed query params (goals router), so
    the fixtures issue one unfrozen request before the frozen test bodies.
    """
    assert test_client.get(GLANCE).status_code == 200


@pytest.fixture
def client(app):
    """The shared api-test app (briefing router included), lifespan running."""
    with TestClient(app) as test_client:
        _prime_routes(test_client)
        yield test_client


# ---------------------------------------------------------------------------
# Seeded payload — every value hand-computed
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(session):
    """Seed all four data sources around the frozen now 2026-07-09 14:23 UTC.

    Energy   : persisted windows 08:00->71, 13:00->64, 14:00->58 (current);
               a 2026-07-08 23:00 and a 2026-07-10 01:00 row prove day scoping.
    Blocks   : past event (ends 10:00), ongoing agent event 14:00-15:00 linked
               to a HIGH task, accepted proposal 15:00-15:45 (MED task),
               titleless event 16:00-16:30, tomorrow event (4th -> dropped);
               a pending and a past-accepted proposal never appear.
    Alerts   : pushed fires 13:50 (top) + 09:00; a suppressed 14:00 fire and a
               pushed fire 2 days ago are excluded -> unresolved_count 2.
    Decisions: alert-kind 09:05 and 13:55 (earliest at/after 13:50 wins the
               top link), schedule_change 14:10 = latest overall.
    """
    task_deep = Task(id=TASK_DEEP_ID, title="Ship revenue model", energy_demand=EnergyDemand.HIGH)
    task_report = Task(
        id=TASK_REPORT_ID, title="Write weekly report", energy_demand=EnergyDemand.MED
    )
    session.add_all([task_deep, task_report])
    session.flush()

    session.add_all(
        [
            _estimate(_utc(8, 23), 40),  # 2026-07-08 23:00 — yesterday, excluded
            _estimate(_utc(9, 8), 71),
            _estimate(_utc(9, 13), 64),
            _estimate(_utc(9, 14), 58),  # current hour
            _estimate(_utc(10, 1), 90),  # tomorrow, excluded
        ]
    )

    session.add_all(
        [
            CalendarEventMirror(
                external_id="standup",
                calendar_source=CalendarSource.GOOGLE,
                summary="Standup",
                start_at=_utc(9, 9),
                end_at=_utc(9, 10),  # already over
            ),
            CalendarEventMirror(
                external_id="deep-work",
                calendar_source=CalendarSource.GOOGLE,
                summary="Deep work block",
                start_at=_utc(9, 14),
                end_at=_utc(9, 15),  # ongoing
                is_agent_created=True,
                agent_task_id=TASK_DEEP_ID,
            ),
            CalendarEventMirror(
                external_id="untitled",
                calendar_source=CalendarSource.CALDAV,
                summary=None,
                start_at=_utc(9, 16),
                end_at=_utc(9, 16, 30),
            ),
            CalendarEventMirror(
                external_id="clinic-tomorrow",
                calendar_source=CalendarSource.GOOGLE,
                summary="Clinic",
                start_at=_utc(10, 9),
                end_at=_utc(10, 10),  # 4th upcoming block -> beyond the top 3
            ),
        ]
    )

    session.add_all(
        [
            ScheduleProposal(
                task_id=TASK_REPORT_ID,
                proposed_start=_utc(9, 15),
                proposed_end=_utc(9, 15, 45),
                status=ProposalStatus.ACCEPTED,
            ),
            ScheduleProposal(  # still pending the confirm gate -> excluded
                task_id=TASK_REPORT_ID,
                proposed_start=_utc(9, 17),
                proposed_end=_utc(9, 17, 30),
                status=ProposalStatus.PROPOSED,
            ),
            ScheduleProposal(  # accepted but already over -> excluded
                task_id=TASK_REPORT_ID,
                proposed_start=_utc(9, 7),
                proposed_end=_utc(9, 8),
                status=ProposalStatus.ACCEPTED,
            ),
        ]
    )

    session.add_all(
        [
            TriggerEvent(
                fired_at=_utc(9, 13, 50),
                rule_id="stress_spike_vs_baseline",
                payload={"summary": "Stress 82 vs baseline 55", "proposal": "Take a break"},
                alert_sent=True,
                dedup_key="stress:2026-07-09",
            ),
            TriggerEvent(
                fired_at=_utc(9, 9),
                rule_id="low_battery_heavy_afternoon",
                payload={"summary": "Body battery 21 before a heavy afternoon"},
                alert_sent=True,
                dedup_key="battery:2026-07-09",
            ),
            TriggerEvent(  # suppressed (never pushed) -> not an alert
                fired_at=_utc(9, 14),
                rule_id="deadline_risk",
                payload={"summary": "suppressed", "push": {"suppressed_reason": "budget"}},
                alert_sent=False,
                dedup_key="deadline:2026-07-09",
            ),
            TriggerEvent(  # pushed but older than 24h -> no longer "unresolved"
                fired_at=_utc(7, 10),
                rule_id="stress_spike_vs_baseline",
                payload={"summary": "old"},
                alert_sent=True,
                dedup_key="stress:2026-07-07",
            ),
        ]
    )

    session.add_all(
        [
            DecisionRecord(
                id=DECISION_ALERT_OLD_ID,
                kind=DecisionKind.ALERT,
                tree={"type": "input", "label": "morning alert"},
                summary="Reasoning for the 09:00 alert",
                created_at=_utc(9, 9, 5),
            ),
            DecisionRecord(
                id=DECISION_ALERT_TOP_ID,
                kind=DecisionKind.ALERT,
                tree={"type": "input", "label": "stress alert"},
                summary="Reasoning for the 13:50 alert",
                created_at=_utc(9, 13, 55),
            ),
            DecisionRecord(
                id=DECISION_SCHEDULE_ID,
                kind=DecisionKind.SCHEDULE_CHANGE,
                tree={"type": "input", "label": "moved block"},
                summary="Moved the deep-work block",
                created_at=_utc(9, 14, 10),
            ),
        ]
    )
    session.commit()


@freeze_time(FROZEN_NOW)
def test_seeded_glance_returns_exact_payload(client, seeded):
    response = client.get(GLANCE)

    assert response.status_code == 200
    hour_scores = {8: 71, 13: 64, 14: 58}
    assert response.json() == {
        "generated_at": "2026-07-09T14:23:00Z",
        "timezone": "UTC",
        "energy": {
            "score": 58,  # the persisted 14:00 window (covers now)
            "confidence": "high",
            "curve_24h": [
                {"hour": hour, "score": hour_scores.get(hour)} for hour in range(24)
            ],
        },
        "next_blocks": [
            {
                "start": "2026-07-09T14:00:00Z",
                "end": "2026-07-09T15:00:00Z",
                "title": "Deep work block",
                "energy_demand": "high",
                "source": "calendar",
            },
            {
                "start": "2026-07-09T15:00:00Z",
                "end": "2026-07-09T15:45:00Z",
                "title": "Write weekly report",
                "energy_demand": "med",
                "source": "proposal",
            },
            {
                "start": "2026-07-09T16:00:00Z",
                "end": "2026-07-09T16:30:00Z",
                "title": None,
                "energy_demand": None,
                "source": "calendar",
            },
        ],
        "alerts": {
            "unresolved_count": 2,
            "top": {
                "rule_id": "stress_spike_vs_baseline",
                "summary": "Stress 82 vs baseline 55",
                "decision_url": f"{BASE_URL}/decisions/{DECISION_ALERT_TOP_ID}",
            },
        },
        "latest_decision": {
            "id": str(DECISION_SCHEDULE_ID),
            "url": f"{BASE_URL}/decisions/{DECISION_SCHEDULE_ID}",
        },
    }


@freeze_time(FROZEN_NOW)
def test_empty_database_yields_valid_all_null_shape(client):
    response = client.get(GLANCE)

    assert response.status_code == 200
    assert response.json() == {
        "generated_at": "2026-07-09T14:23:00Z",
        "timezone": "UTC",
        "energy": {
            "score": None,
            "confidence": "low",
            "curve_24h": [{"hour": hour, "score": None} for hour in range(24)],
        },
        "next_blocks": [],
        "alerts": {"unresolved_count": 0, "top": None},
        "latest_decision": None,
    }


@freeze_time(FROZEN_NOW)
def test_alert_without_payload_or_decision_degrades_honestly(client, session):
    # Legacy/threadbare rows: no payload -> the rule id is the summary; no
    # alert-kind decision recorded after the fire -> decision_url null.
    session.add(
        TriggerEvent(
            fired_at=_utc(9, 13, 0),
            rule_id="schedule_changed",
            payload=None,
            alert_sent=True,
            dedup_key="sched:2026-07-09",
        )
    )
    # An alert decision from BEFORE the fire must not be claimed by it.
    session.add(
        DecisionRecord(
            kind=DecisionKind.ALERT,
            tree={"type": "input", "label": "n"},
            summary="earlier alert reasoning",
            created_at=_utc(9, 12, 0),
        )
    )
    session.commit()

    body = client.get(GLANCE).json()

    assert body["alerts"]["unresolved_count"] == 1
    assert body["alerts"]["top"]["rule_id"] == "schedule_changed"
    assert body["alerts"]["top"]["summary"] == "schedule_changed"
    assert body["alerts"]["top"]["decision_url"] is None


# ---------------------------------------------------------------------------
# Confidence ladder (freshness of the latest persisted window)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        ("2026-07-09 09:30:00", "high"),  # inside the persisted 09:00 window
        ("2026-07-09 11:30:00", "medium"),  # 2.5 h stale (<= 3 h)
        ("2026-07-09 14:23:00", "low"),  # 5+ h stale
    ],
)
def test_confidence_reflects_staleness_of_latest_window(client, session, at, expected):
    session.add(_estimate(_utc(9, 9), 50))
    session.commit()

    with freeze_time(at):
        body = client.get(GLANCE).json()

    assert body["energy"]["score"] == 50
    assert body["energy"]["confidence"] == expected


# ---------------------------------------------------------------------------
# Local-day mapping (curve hours are the user's wall-clock hours)
# ---------------------------------------------------------------------------


@pytest.fixture
def seoul_client(settings, session_factory):
    seoul = settings.model_copy(update={"timezone": "Asia/Seoul"})
    application = create_app(seoul)

    def _override_get_session():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_session] = _override_get_session
    with TestClient(application) as test_client:
        _prime_routes(test_client)
        yield test_client


@freeze_time("2026-07-09 03:23:00")  # 12:23 in Seoul (UTC+9)
def test_curve_hours_follow_the_user_timezone(seoul_client, session):
    session.add_all(
        [
            _estimate(_utc(8, 14), 45),  # 23:00 KST on 2026-07-08 -> previous local day
            _estimate(_utc(8, 15), 80),  # local hour 0
            _estimate(_utc(9, 0), 66),  # local hour 9
            _estimate(_utc(9, 3), 61),  # local hour 12 (current)
        ]
    )
    session.commit()

    body = seoul_client.get(GLANCE).json()

    assert body["timezone"] == "Asia/Seoul"
    scores = {point["hour"]: point["score"] for point in body["energy"]["curve_24h"]}
    assert len(body["energy"]["curve_24h"]) == 24
    assert scores[0] == 80
    assert scores[9] == 66
    assert scores[12] == 61
    assert all(scores[hour] is None for hour in range(24) if hour not in {0, 9, 12})
    assert body["energy"]["score"] == 61
    assert body["energy"]["confidence"] == "high"


# ---------------------------------------------------------------------------
# Cache-Control / ETag / 304 (widget polling budget)
# ---------------------------------------------------------------------------


class TestConditionalGet:
    def test_response_carries_cache_headers_and_strong_etag(self, client):
        with freeze_time(FROZEN_NOW):
            response = client.get(GLANCE)

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "private, max-age=300"
        etag = response.headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"') and len(etag) == 66  # sha-256 hex

    def test_if_none_match_returns_304_with_headers_and_no_body(self, client):
        with freeze_time(FROZEN_NOW):
            etag = client.get(GLANCE).headers["ETag"]
            response = client.get(GLANCE, headers={"If-None-Match": etag})

        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["ETag"] == etag
        assert response.headers["Cache-Control"] == "private, max-age=300"

    def test_etag_ignores_generated_at_so_unchanged_data_revalidates(self, client):
        with freeze_time("2026-07-09 14:23:00"):
            first = client.get(GLANCE)
        with freeze_time("2026-07-09 14:24:30"):
            second = client.get(GLANCE)
            revalidated = client.get(GLANCE, headers={"If-None-Match": first.headers["ETag"]})

        assert first.json()["generated_at"] != second.json()["generated_at"]
        assert first.headers["ETag"] == second.headers["ETag"]
        assert revalidated.status_code == 304

    def test_weak_prefix_and_lists_match(self, client):
        with freeze_time(FROZEN_NOW):
            etag = client.get(GLANCE).headers["ETag"]
            weak = client.get(GLANCE, headers={"If-None-Match": f"W/{etag}"})
            listed = client.get(GLANCE, headers={"If-None-Match": f'"nope", {etag}'})

        assert weak.status_code == 304
        assert listed.status_code == 304

    def test_data_change_invalidates_etag(self, client, session):
        with freeze_time(FROZEN_NOW):
            stale = client.get(GLANCE).headers["ETag"]
            session.add(_estimate(_utc(9, 14), 58))
            session.commit()
            response = client.get(GLANCE, headers={"If-None-Match": stale})

        assert response.status_code == 200
        assert response.headers["ETag"] != stale

    def test_non_matching_etag_returns_full_body(self, client):
        with freeze_time(FROZEN_NOW):
            response = client.get(GLANCE, headers={"If-None-Match": '"bogus"'})

        assert response.status_code == 200
        assert response.json()["alerts"] == {"unresolved_count": 0, "top": None}


# ---------------------------------------------------------------------------
# Auth: same bearer gate as the rest of /v1 (loopback-open when no token)
# ---------------------------------------------------------------------------

TOKEN = "briefing-test-token-123"


@contextmanager
def _secured_client(settings):
    """Real app factory + auth middleware (test_auth style); briefing is wired in."""
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    application = create_app(secured)
    with TestClient(application) as test_client:
        Base.metadata.create_all(get_engine())
        yield test_client


class TestAuth:
    def test_rejected_without_token(self, settings):
        with _secured_client(settings) as client:
            response = client.get(GLANCE)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_allowed_with_bearer_token(self, settings):
        with _secured_client(settings) as client:
            response = client.get(GLANCE, headers={"Authorization": f"Bearer {TOKEN}"})

        assert response.status_code == 200

    def test_viewer_query_token_never_authorizes_the_briefing(self, settings):
        # The derived ?token= credential is for viewer pages only; the glance
        # payload carries health context and stays bearer-gated.
        with _secured_client(settings) as client:
            response = client.get(GLANCE, params={"token": viewer_token(TOKEN)})

        assert response.status_code == 401

    def test_decision_urls_embed_the_derived_viewer_token(self, settings):
        with _secured_client(settings) as client:
            with get_session_factory()() as session:
                session.add(
                    DecisionRecord(
                        id=DECISION_SCHEDULE_ID,
                        kind=DecisionKind.SCHEDULE_CHANGE,
                        tree={"type": "input", "label": "n"},
                        summary="s",
                    )
                )
                session.commit()
            body = client.get(GLANCE, headers={"Authorization": f"Bearer {TOKEN}"}).json()

        expected = (
            f"{BASE_URL}/decisions/{DECISION_SCHEDULE_ID}?token={viewer_token(TOKEN)}"
        )
        assert body["latest_decision"]["url"] == expected
        assert TOKEN not in body["latest_decision"]["url"]
