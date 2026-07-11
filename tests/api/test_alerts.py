"""Tests for GET /v1/alerts (issue #10): the apps' alert-history list.

The endpoint must carry the §8.5 notification-grammar lines recorded in each
pushed trigger event's payload and must NEVER disagree with the glance
``alerts`` block (same recency window, same ordering, same decision-link
heuristic) — one test asserts glance/list agreement directly.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from healthmes.store import DecisionKind, DecisionRecord, TriggerEvent

FROZEN_NOW = "2026-07-09 14:23:00"
ALERTS = "/v1/alerts"

DECISION_EARLY_ID = uuid.UUID("00000000-0000-0000-0000-00000000e001")
DECISION_TOP_ID = uuid.UUID("00000000-0000-0000-0000-00000000e002")


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def _event(
    fired_at: datetime,
    rule_id: str,
    *,
    sent: bool = True,
    payload: dict | None = None,
) -> TriggerEvent:
    return TriggerEvent(
        fired_at=fired_at,
        rule_id=rule_id,
        payload=payload,
        alert_sent=sent,
        dedup_key=f"{rule_id}:{fired_at.isoformat()}",
    )


def _payload(summary: str, proposal: str = "Move the 14:00 block to tomorrow.") -> dict:
    return {
        "summary": summary,
        "proposal": proposal,
        "evidence": {"hrv_delta_pct": -18, "baseline_days": 14},
        "push": {"sent": True, "status_code": 200},
    }


@pytest.fixture
def client(app):
    """Shared api-test app; one unfrozen priming request (see test_briefing)."""
    with TestClient(app) as test_client:
        assert test_client.get(ALERTS).status_code == 200
        yield test_client


@contextmanager
def frozen():
    with freeze_time(FROZEN_NOW):
        yield


@pytest.fixture
def seeded(session):
    """Two pushed alerts inside 24 h; suppressed + stale ones excluded."""
    session.add_all(
        [
            _event(_utc(9, 13, 50), "deep_sleep_drop", payload=_payload("Recovery 38 today.")),
            _event(_utc(9, 9, 0), "schedule_overload", payload=_payload("4 high blocks today.")),
            # Fired but never pushed: not an alert the user ever saw.
            _event(_utc(9, 14, 0), "suppressed_rule", sent=False, payload=_payload("Hidden")),
            # Pushed but older than the 24 h window.
            _event(_utc(7, 10, 0), "stale_rule", payload=_payload("Old news.")),
        ]
    )
    decision_early = DecisionRecord(
        id=DECISION_EARLY_ID,
        kind=DecisionKind.ALERT,
        tree={"id": "root", "type": "rule", "label": "early", "children": []},
        summary="Alert reasoning (09:05)",
    )
    decision_early.created_at = _utc(9, 9, 5)
    decision_top = DecisionRecord(
        id=DECISION_TOP_ID,
        kind=DecisionKind.ALERT,
        tree={"id": "root", "type": "rule", "label": "top", "children": []},
        summary="Alert reasoning (13:55)",
    )
    decision_top.created_at = _utc(9, 13, 55)
    # A non-alert decision must never be linked from an alert.
    unrelated = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        tree={"id": "root", "type": "action", "label": "move", "children": []},
        summary="Schedule change",
    )
    unrelated.created_at = _utc(9, 14, 10)
    session.add_all([decision_early, decision_top, unrelated])
    session.commit()


def test_lists_recent_pushed_alerts_newest_first(client, seeded, parse_utc):
    with frozen():
        response = client.get(ALERTS)

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["total_count"] == 2
    assert [alert["rule_id"] for alert in body["data"]] == [
        "deep_sleep_drop",
        "schedule_overload",
    ]

    top = body["data"][0]
    assert top["summary"] == "Recovery 38 today."
    assert top["proposal"] == "Move the 14:00 block to tomorrow."
    assert top["evidence"] == {"hrv_delta_pct": -18, "baseline_days": 14}
    assert parse_utc(top["fired_at"]) == _utc(9, 13, 50)
    assert uuid.UUID(top["id"])  # a real trigger_event id the app can key on


def test_decision_links_use_the_glance_heuristic(client, seeded):
    with frozen():
        alerts = client.get(ALERTS).json()["data"]
        glance_alerts = client.get("/v1/briefing/glance").json()["alerts"]

    # Earliest alert-kind decision at/after each fire (no FK yet).
    assert alerts[0]["decision_url"].endswith(f"/decisions/{DECISION_TOP_ID}")
    assert alerts[1]["decision_url"].endswith(f"/decisions/{DECISION_EARLY_ID}")

    # The list must agree with the glance widget contract verbatim.
    assert glance_alerts["unresolved_count"] == len(alerts) == 2
    assert glance_alerts["top"]["rule_id"] == alerts[0]["rule_id"]
    assert glance_alerts["top"]["summary"] == alerts[0]["summary"]
    assert glance_alerts["top"]["decision_url"] == alerts[0]["decision_url"]


def test_legacy_row_without_payload_falls_back_to_rule_id(client, session):
    session.add(_event(_utc(9, 12, 0), "legacy_rule", payload=None))
    session.commit()

    with frozen():
        (alert,) = client.get(ALERTS).json()["data"]

    assert alert["summary"] == "legacy_rule"
    assert alert["proposal"] is None
    assert alert["evidence"] is None
    assert alert["decision_url"] is None


def test_hours_param_widens_the_window(client, seeded):
    with frozen():
        default = client.get(ALERTS).json()
        widened = client.get(ALERTS, params={"hours": 96}).json()

    assert default["pagination"]["total_count"] == 2
    assert widened["pagination"]["total_count"] == 3
    assert widened["data"][-1]["rule_id"] == "stale_rule"


def test_hours_param_is_bounded(client):
    assert client.get(ALERTS, params={"hours": 0}).status_code == 422
    assert client.get(ALERTS, params={"hours": 24 * 7 + 1}).status_code == 422


def test_pagination_slices_newest_first(client, session):
    for hour in (8, 10, 12):
        session.add(_event(_utc(9, hour, 0), f"rule_{hour}", payload=_payload(f"at {hour}")))
    session.commit()

    with frozen():
        first = client.get(ALERTS, params={"limit": 2}).json()
        rest = client.get(ALERTS, params={"limit": 2, "offset": 2}).json()

    assert [a["rule_id"] for a in first["data"]] == ["rule_12", "rule_10"]
    assert first["pagination"] == {
        "total_count": 3,
        "limit": 2,
        "offset": 0,
        "has_more": True,
    }
    assert [a["rule_id"] for a in rest["data"]] == ["rule_8"]
    assert rest["pagination"]["has_more"] is False


def test_empty_store_returns_empty_page(client):
    body = client.get(ALERTS).json()
    assert body["data"] == []
    assert body["pagination"]["total_count"] == 0
