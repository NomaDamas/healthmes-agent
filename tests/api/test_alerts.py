"""Tests for GET /v1/alerts (issue #10): the apps' alert-history list.

The endpoint must carry the §8.5 notification-grammar lines recorded in each
pushed trigger event's payload and must NEVER disagree with the glance
``alerts`` block (same recency window, same ordering, same persisted
decision correlation) — one test asserts glance/list agreement directly.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from healthmes.store import (
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TaskSource,
    TriggerEvent,
)

FROZEN_NOW = "2026-07-09 14:23:00"
ALERTS = "/v1/alerts"

DECISION_EARLY_ID = uuid.UUID("00000000-0000-0000-0000-00000000e001")
DECISION_TOP_ID = uuid.UUID("00000000-0000-0000-0000-00000000e002")
EVENT_EARLY_ID = uuid.UUID("00000000-0000-0000-0000-00000000f001")
EVENT_TOP_ID = uuid.UUID("00000000-0000-0000-0000-00000000f002")


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
    top_event = _event(
        _utc(9, 13, 50),
        "deep_sleep_drop",
        payload=_payload("Recovery 38 today."),
    )
    top_event.id = EVENT_TOP_ID
    early_event = _event(
        _utc(9, 9, 0),
        "schedule_overload",
        payload=_payload("4 high blocks today."),
    )
    early_event.id = EVENT_EARLY_ID
    session.add_all(
        [
            top_event,
            early_event,
            # Fired but never pushed: not an alert the user ever saw.
            _event(_utc(9, 14, 0), "suppressed_rule", sent=False, payload=_payload("Hidden")),
            # Pushed but older than the 24 h window.
            _event(_utc(7, 10, 0), "stale_rule", payload=_payload("Old news.")),
        ]
    )
    session.flush()
    decision_early = DecisionRecord(
        id=DECISION_EARLY_ID,
        kind=DecisionKind.ALERT,
        tree={"id": "root", "type": "rule", "label": "early", "children": []},
        summary="Alert reasoning (09:05)",
        trigger_event_id=EVENT_EARLY_ID,
    )
    decision_early.created_at = _utc(9, 9, 5)
    decision_top = DecisionRecord(
        id=DECISION_TOP_ID,
        kind=DecisionKind.ALERT,
        tree={"id": "root", "type": "rule", "label": "top", "children": []},
        summary="Alert reasoning (13:55)",
        trigger_event_id=EVENT_TOP_ID,
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


def test_decision_links_use_exact_trigger_correlation(client, seeded):
    with frozen():
        alerts = client.get(ALERTS).json()["data"]
        glance_alerts = client.get("/v1/briefing/glance").json()["alerts"]

    assert alerts[0]["decision_url"].endswith(f"/decisions/{DECISION_TOP_ID}")
    assert alerts[1]["decision_url"].endswith(f"/decisions/{DECISION_EARLY_ID}")

    # The list must agree with the glance widget contract verbatim.
    assert glance_alerts["unresolved_count"] == len(alerts) == 2
    assert glance_alerts["top"]["id"] == alerts[0]["id"]
    assert glance_alerts["top"]["rule_id"] == alerts[0]["rule_id"]
    assert glance_alerts["top"]["summary"] == alerts[0]["summary"]
    assert glance_alerts["top"]["decision_url"] == alerts[0]["decision_url"]


def test_proposal_alert_resolves_its_direct_target_beyond_first_page(
    client,
    session,
):
    event = _event(
        _utc(9, 13, 50),
        "calendar_task_intake",
        payload=_payload("A calendar task needs a block."),
    )
    event.id = uuid.uuid4()
    decision = DecisionRecord(
        kind=DecisionKind.ALERT,
        tree={"id": "root", "type": "action", "label": "schedule", "children": []},
        summary="Schedule the calendar task",
        trigger_event_id=event.id,
    )
    decision.created_at = _utc(9, 13, 55)
    session.add(event)
    session.flush()
    session.add(decision)
    session.flush()

    proposals: list[ScheduleProposal] = []
    base_start = _utc(10, 0)
    for index in range(51):
        task = Task(
            title=f"Task {index}",
            energy_demand=EnergyDemand.MED,
            status="scheduled",
            source=TaskSource.AGENT,
        )
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=base_start + timedelta(minutes=index),
            proposed_end=base_start + timedelta(minutes=index + 30),
            status=ProposalStatus.PROPOSED,
            decision_record_id=decision.id if index == 50 else None,
            reply_handle_digest=f"{index + 1:064x}",
            expires_at=_utc(10, 12),
        )
        session.add(proposal)
        proposals.append(proposal)
    session.commit()
    target_id = proposals[-1].id

    with frozen():
        alert = client.get(ALERTS, params={"limit": 1}).json()["data"][0]
        first_page = client.get(
            "/v1/schedule/proposals",
            params={"status": "proposed", "limit": 50, "offset": 0},
        ).json()
        direct = client.get(f"/v1/schedule/proposals/{target_id}")

    assert alert["proposal_id"] == str(target_id)
    assert str(target_id) not in {row["id"] for row in first_page["data"]}
    assert first_page["pagination"]["total_count"] == 51
    assert direct.status_code == 200
    token = direct.json()["accept_resolution_token"]
    assert token

    with frozen():
        resolved = client.post(
            f"/v1/schedule/proposals/{target_id}/accept",
            json={"resolution_token": token},
        )

    assert resolved.status_code == 200
    assert resolved.json()["status"] == "accepted"


def test_expired_proposal_is_not_exposed_as_an_alert_action(client, session):
    event = _event(
        _utc(9, 13, 50),
        "calendar_task_intake",
        payload=_payload("An expired calendar task proposal."),
    )
    decision = DecisionRecord(
        kind=DecisionKind.ALERT,
        tree={"id": "root", "type": "action", "label": "schedule", "children": []},
        summary="Expired schedule proposal",
        trigger_event_id=event.id,
    )
    task = Task(
        title="Expired task",
        energy_demand=EnergyDemand.MED,
        status="scheduled",
        source=TaskSource.AGENT,
    )
    session.add_all([event, decision, task])
    session.flush()
    session.add(
        ScheduleProposal(
            task_id=task.id,
            proposed_start=_utc(10, 9),
            proposed_end=_utc(10, 10),
            status=ProposalStatus.PROPOSED,
            decision_record_id=decision.id,
            reply_handle_digest="a" * 64,
            expires_at=_utc(9, 14, 22),
        )
    )
    session.commit()

    with frozen():
        (alert,) = client.get(ALERTS).json()["data"]

    assert alert["id"] == str(event.id)
    assert alert["proposal_id"] is None


def test_multiple_alerts_keep_exact_proposals_when_decisions_finish_out_of_order(
    client,
    session,
):
    first_event = _event(
        _utc(9, 13, 40),
        "calendar_task_intake",
        payload=_payload("First calendar task."),
    )
    second_event = _event(
        _utc(9, 13, 50),
        "deadline_risk",
        payload=_payload("Second calendar task."),
    )
    first_event.id = uuid.uuid4()
    second_event.id = uuid.uuid4()
    session.add_all([first_event, second_event])
    session.flush()

    # The later alert finishes first. Temporal matching would assign this
    # decision to both alerts; persisted trigger IDs must keep them separate.
    second_decision = DecisionRecord(
        kind=DecisionKind.ALERT,
        tree={"id": "second", "type": "action", "label": "second", "children": []},
        summary="Second alert finished first",
        trigger_event_id=second_event.id,
        created_at=_utc(9, 13, 55),
    )
    first_decision = DecisionRecord(
        kind=DecisionKind.ALERT,
        tree={"id": "first", "type": "action", "label": "first", "children": []},
        summary="First alert finished later",
        trigger_event_id=first_event.id,
        created_at=_utc(9, 14, 0),
    )
    session.add_all([first_decision, second_decision])
    session.flush()

    proposals: dict[uuid.UUID, uuid.UUID] = {}
    for index, decision in enumerate((first_decision, second_decision)):
        task = Task(
            title=f"Correlated task {index}",
            energy_demand=EnergyDemand.MED,
            status="scheduled",
            source=TaskSource.AGENT,
        )
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=_utc(10, 9 + index),
            proposed_end=_utc(10, 10 + index),
            status=ProposalStatus.PROPOSED,
            decision_record_id=decision.id,
            reply_handle_digest=f"{index + 100:064x}",
            expires_at=_utc(10, 12),
        )
        session.add(proposal)
        session.flush()
        proposals[decision.id] = proposal.id
    session.commit()

    with frozen():
        alerts = client.get(ALERTS).json()["data"]

    by_event = {uuid.UUID(alert["id"]): alert for alert in alerts}
    assert by_event[first_event.id]["proposal_id"] == str(proposals[first_decision.id])
    assert by_event[second_event.id]["proposal_id"] == str(proposals[second_decision.id])
    assert by_event[first_event.id]["decision_url"].endswith(
        f"/decisions/{first_decision.id}"
    )
    assert by_event[second_event.id]["decision_url"].endswith(
        f"/decisions/{second_decision.id}"
    )


def test_legacy_row_without_payload_falls_back_to_rule_id(client, session):
    event = _event(_utc(9, 12, 0), "legacy_rule", payload=None)
    session.add(event)
    session.add(
        DecisionRecord(
            kind=DecisionKind.ALERT,
            tree={"id": "later", "type": "rule", "label": "later", "children": []},
            summary="Uncorrelated legacy decision",
            created_at=_utc(9, 12, 5),
        )
    )
    session.commit()

    with frozen():
        (alert,) = client.get(ALERTS).json()["data"]

    assert alert["summary"] == "legacy_rule"
    assert alert["proposal"] is None
    assert alert["evidence"] is None
    assert alert["decision_url"] is None
    assert alert["proposal_id"] is None


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
