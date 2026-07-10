"""Tests for the schedule router (calendar mirror range list + proposals)."""

from datetime import UTC, datetime

from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    ProposalStatus,
    ScheduleProposal,
    Task,
)


def _dt(hour: int, minute: int = 0, day: int = 6) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def _seed_events(session):
    events = [
        CalendarEventMirror(
            external_id="inside",
            calendar_source=CalendarSource.GOOGLE,
            summary="Inside",
            start_at=_dt(10),
            end_at=_dt(11),
        ),
        CalendarEventMirror(
            external_id="overlaps-start",
            calendar_source=CalendarSource.CALDAV,
            summary="Overlaps range start",
            start_at=_dt(8),
            end_at=_dt(9, 30),
        ),
        CalendarEventMirror(
            external_id="outside",
            calendar_source=CalendarSource.GOOGLE,
            summary="After range",
            start_at=_dt(18),
            end_at=_dt(19),
        ),
    ]
    session.add_all(events)
    session.commit()


def test_list_events_returns_overlapping_range_ordered(client, session):
    _seed_events(session)

    response = client.get(
        "/v1/schedule/events",
        params={"start": "2026-07-06T09:00:00Z", "end": "2026-07-06T12:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [e["external_id"] for e in body["data"]] == ["overlaps-start", "inside"]
    assert body["pagination"]["total_count"] == 2
    assert body["data"][0]["calendar_source"] == "caldav"


def test_list_events_filters_by_calendar_source(client, session):
    _seed_events(session)

    response = client.get(
        "/v1/schedule/events",
        params={
            "start": "2026-07-06T00:00:00Z",
            "end": "2026-07-07T00:00:00Z",
            "calendar_source": "google",
        },
    )

    assert [e["external_id"] for e in response.json()["data"]] == ["inside", "outside"]


def test_list_events_requires_start_and_end(client):
    response = client.get("/v1/schedule/events")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_events_rejects_inverted_range(client):
    response = client.get(
        "/v1/schedule/events",
        params={"start": "2026-07-06T12:00:00Z", "end": "2026-07-06T09:00:00Z"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_range"


def _seed_proposal(session) -> ScheduleProposal:
    task = Task(title="Deep work block")
    session.add(task)
    session.flush()
    proposal = ScheduleProposal(
        task_id=task.id,
        proposed_start=_dt(9),
        proposed_end=_dt(11),
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    return proposal


def test_list_proposals_filters_by_status(client, session):
    proposal = _seed_proposal(session)

    listed = client.get("/v1/schedule/proposals", params={"status": "proposed"}).json()
    assert [p["id"] for p in listed["data"]] == [str(proposal.id)]

    empty = client.get("/v1/schedule/proposals", params={"status": "accepted"}).json()
    assert empty["data"] == []
    assert empty["pagination"]["total_count"] == 0


def test_accept_proposal_then_second_accept_conflicts(client, session):
    proposal = _seed_proposal(session)

    accepted = client.post(f"/v1/schedule/proposals/{proposal.id}/accept")
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status == ProposalStatus.ACCEPTED

    again = client.post(f"/v1/schedule/proposals/{proposal.id}/accept")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_transition"


def test_decline_proposal(client, session):
    proposal = _seed_proposal(session)

    declined = client.post(f"/v1/schedule/proposals/{proposal.id}/decline")

    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"


def test_proposal_actions_404_for_unknown_id(client):
    response = client.post("/v1/schedule/proposals/00000000-0000-0000-0000-000000000000/accept")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
