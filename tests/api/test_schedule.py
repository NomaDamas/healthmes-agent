"""Tests for the schedule router (calendar mirror range list + proposals)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from healthmes.calendars.adjustments import issue_reply_handle
from healthmes.schedule_proposals import (
    ScheduleProposalResolutionError,
    resolve_schedule_proposal,
)
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    ProposalStatus,
    ScheduleProposal,
    Task,
)

HANDLE_SECRET = "test-calendar-adjustment-secret-32-characters"


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


def _seed_proposal_with_handle(session) -> tuple[ScheduleProposal, str]:
    task = Task(title="Deep work block")
    session.add(task)
    session.flush()
    handle = issue_reply_handle(HANDLE_SECRET)
    proposal = ScheduleProposal(
        task_id=task.id,
        proposed_start=_dt(9),
        proposed_end=_dt(11),
        reply_handle_digest=handle.digest,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    return proposal, handle.plaintext


def _seed_proposal(session) -> ScheduleProposal:
    proposal, _handle = _seed_proposal_with_handle(session)
    return proposal


def _resolution_token(
    client,
    proposal: ScheduleProposal,
    *,
    action: str = "accept",
) -> str:
    response = client.get(
        "/v1/schedule/proposals",
        params={"status": "proposed", "task_id": str(proposal.task_id)},
    )
    return response.json()["data"][0][f"{action}_resolution_token"]


def test_list_proposals_filters_by_status(client, session):
    proposal = _seed_proposal(session)

    listed = client.get("/v1/schedule/proposals", params={"status": "proposed"}).json()
    assert [p["id"] for p in listed["data"]] == [str(proposal.id)]
    assert listed["data"][0]["accept_resolution_token"]
    assert listed["data"][0]["decline_resolution_token"]
    assert (
        listed["data"][0]["accept_resolution_token"]
        != listed["data"][0]["decline_resolution_token"]
    )

    empty = client.get("/v1/schedule/proposals", params={"status": "accepted"}).json()
    assert empty["data"] == []
    assert empty["pagination"]["total_count"] == 0


def test_accept_proposal_then_second_accept_conflicts(client, session):
    proposal = _seed_proposal(session)
    token = _resolution_token(client, proposal)

    accepted = client.post(
        f"/v1/schedule/proposals/{proposal.id}/accept",
        json={"resolution_token": token},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status == ProposalStatus.ACCEPTED

    again = client.post(
        f"/v1/schedule/proposals/{proposal.id}/accept",
        json={"resolution_token": token},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_transition"


def test_accept_invalidates_proposal_when_actual_sleep_changed(client, session):
    proposal = _seed_proposal(session)
    proposal.proposed_start = _dt(6)
    proposal.proposed_end = _dt(7)
    session.add(
        CalendarEventMirror(
            external_id="actual-sleep",
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=_dt(23, day=5),
            end_at=_dt(7, 30),
            is_agent_created=True,
            healthmes_kind="actual_sleep",
            sleep_local_date=_dt(7).date(),
        )
    )
    session.commit()
    token = _resolution_token(client, proposal)

    response = client.post(
        f"/v1/schedule/proposals/{proposal.id}/accept",
        json={"resolution_token": token},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "actual_sleep_conflict"
    assert response.json()["error"]["detail"] == {
        "proposal_status": "invalidated"
    }
    session.expire_all()
    assert (
        session.get(ScheduleProposal, proposal.id).status
        is ProposalStatus.INVALIDATED
    )


def test_decline_proposal(client, session):
    proposal = _seed_proposal(session)
    token = _resolution_token(client, proposal, action="decline")

    declined = client.post(
        f"/v1/schedule/proposals/{proposal.id}/decline",
        json={"resolution_token": token},
    )

    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"


def test_proposal_actions_404_for_unknown_id(client):
    response = client.post(
        "/v1/schedule/proposals/00000000-0000-0000-0000-000000000000/accept",
        json={"resolution_token": "unused-for-missing-proposal"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_proposal_actions_require_valid_unexpired_resolution_token(client, session):
    proposal = _seed_proposal(session)
    endpoint = f"/v1/schedule/proposals/{proposal.id}/accept"

    missing = client.post(endpoint)
    assert missing.status_code == 422

    invalid = client.post(endpoint, json={"resolution_token": "wrong-token"})
    assert invalid.status_code == 403
    assert invalid.json()["error"]["code"] == "invalid_resolution_token"

    proposal.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    expired_token = _resolution_token(client, proposal)
    expired = client.post(endpoint, json={"resolution_token": expired_token})
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "proposal_expired"

    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.PROPOSED


def test_resolution_tokens_are_action_and_proposal_scoped(client, session):
    first = _seed_proposal(session)
    second = _seed_proposal(session)
    first_accept = _resolution_token(client, first, action="accept")
    first_decline = _resolution_token(client, first, action="decline")

    wrong_action = client.post(
        f"/v1/schedule/proposals/{first.id}/decline",
        json={"resolution_token": first_accept},
    )
    assert wrong_action.status_code == 403
    assert wrong_action.json()["error"]["code"] == "invalid_resolution_token"

    wrong_proposal = client.post(
        f"/v1/schedule/proposals/{second.id}/accept",
        json={"resolution_token": first_accept},
    )
    assert wrong_proposal.status_code == 403
    assert wrong_proposal.json()["error"]["code"] == "invalid_resolution_token"

    declined = client.post(
        f"/v1/schedule/proposals/{first.id}/decline",
        json={"resolution_token": first_decline},
    )
    assert declined.status_code == 200

    replay = client.post(
        f"/v1/schedule/proposals/{first.id}/decline",
        json={"resolution_token": first_decline},
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "invalid_transition"


def test_direct_proposal_lookup_reaches_beyond_first_page_and_is_not_cached(
    client,
    session,
):
    proposals = [_seed_proposal(session) for _ in range(51)]
    target = proposals[-1]
    target.proposed_start = _dt(17)
    target.proposed_end = _dt(18)
    session.commit()

    first_page = client.get(
        "/v1/schedule/proposals",
        params={"status": "proposed", "limit": 50, "offset": 0},
    )
    assert first_page.status_code == 200
    assert first_page.headers["cache-control"] == "no-store"
    assert str(target.id) not in {row["id"] for row in first_page.json()["data"]}

    direct = client.get(f"/v1/schedule/proposals/{target.id}")
    assert direct.status_code == 200
    assert direct.headers["cache-control"] == "no-store"
    assert direct.json()["id"] == str(target.id)
    assert direct.json()["accept_resolution_token"]
    assert direct.json()["decline_resolution_token"]


def test_rest_resolution_token_is_not_a_telegram_reply_handle(client, session):
    proposal = _seed_proposal(session)
    token = _resolution_token(client, proposal)

    with pytest.raises(ScheduleProposalResolutionError, match="invalid_handle"):
        resolve_schedule_proposal(
            session,
            proposal.id,
            ProposalStatus.ACCEPTED,
            token,
            HANDLE_SECRET,
        )

    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.PROPOSED


def test_rest_action_rejects_telegram_reply_handle(client, session):
    proposal, reply_handle = _seed_proposal_with_handle(session)

    response = client.post(
        f"/v1/schedule/proposals/{proposal.id}/accept",
        json={"resolution_token": reply_handle},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_resolution_token"
    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.PROPOSED


def test_proposal_actions_fail_closed_without_handle_secret(client, app, session):
    proposal = _seed_proposal(session)
    token = _resolution_token(client, proposal)
    app.state.settings = app.state.settings.model_copy(
        update={"calendar_adjustment_secret": SecretStr("")}
    )

    response = client.post(
        f"/v1/schedule/proposals/{proposal.id}/accept",
        json={"resolution_token": token},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "approval_unavailable"
    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.PROPOSED
