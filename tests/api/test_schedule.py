"""Tests for the schedule router (calendar mirror range list + proposals)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from pydantic import SecretStr

from healthmes.api import schedule as schedule_api
from healthmes.calendars import creds
from healthmes.calendars.adjustments import issue_reply_handle
from healthmes.calendars.state import FileSyncHealthStore
from healthmes.schedule_proposals import (
    ScheduleProposalResolutionError,
    invalidate_schedule_proposal,
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
GOOGLE_ACCOUNT_GENERATION = "a" * 32
CALDAV_ACCOUNT_GENERATION = "b" * 32
RECONNECTED_GOOGLE_ACCOUNT_GENERATION = "c" * 32


def _dt(hour: int, minute: int = 0, day: int = 6) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def _connect_google(
    client,
    *,
    generation: str = GOOGLE_ACCOUNT_GENERATION,
) -> None:
    token_path = client.app.state.settings.data_dir / "google" / "calendar_token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": "fake-refresh",
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "fake-secret",
                "_healthmes_account_generation": generation,
            }
        ),
        encoding="utf-8",
    )


def _mark_calendar_synced(
    client,
    source: CalendarSource,
    generation: str,
    *,
    event_count: int = 1,
) -> None:
    FileSyncHealthStore.for_data_dir(client.app.state.settings.data_dir).record_success(
        source,
        _dt(8),
        event_count=event_count,
        account_generation=generation,
    )


def _make_calendars_visible(client) -> None:
    _connect_google(client)
    creds.save_caldav_credentials(
        client.app.state.settings.data_dir,
        username="calendar@example.test",
        app_password="test-app-password",
        url="https://caldav.test",
        account_generation=CALDAV_ACCOUNT_GENERATION,
    )
    _mark_calendar_synced(
        client,
        CalendarSource.GOOGLE,
        GOOGLE_ACCOUNT_GENERATION,
        event_count=2,
    )
    _mark_calendar_synced(
        client,
        CalendarSource.CALDAV,
        CALDAV_ACCOUNT_GENERATION,
    )


def _seed_events(session):
    events = [
        CalendarEventMirror(
            external_id="inside",
            calendar_source=CalendarSource.GOOGLE,
            connection_generation=GOOGLE_ACCOUNT_GENERATION,
            summary="Inside",
            start_at=_dt(10),
            end_at=_dt(11),
        ),
        CalendarEventMirror(
            external_id="overlaps-start",
            calendar_source=CalendarSource.CALDAV,
            connection_generation=CALDAV_ACCOUNT_GENERATION,
            summary="Overlaps range start",
            start_at=_dt(8),
            end_at=_dt(9, 30),
        ),
        CalendarEventMirror(
            external_id="outside",
            calendar_source=CalendarSource.GOOGLE,
            connection_generation=GOOGLE_ACCOUNT_GENERATION,
            summary="After range",
            start_at=_dt(18),
            end_at=_dt(19),
        ),
    ]
    session.add_all(events)
    session.commit()


def test_list_events_returns_overlapping_range_ordered(client, session):
    _make_calendars_visible(client)
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
    _make_calendars_visible(client)
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


def test_list_events_fails_closed_when_calendar_is_disconnected(
    client,
    session,
):
    _seed_events(session)

    response = client.get(
        "/v1/schedule/events",
        params={
            "start": "2026-07-06T00:00:00Z",
            "end": "2026-07-07T00:00:00Z",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "calendar_unavailable",
        "message": (
            "Calendar events are unavailable until the connected account "
            "completes a successful sync."
        ),
        "detail": {"reason_codes": ["calendar_not_connected"]},
    }


def test_list_events_hides_old_generation_after_unsynced_reconnect(
    client,
    session,
):
    _connect_google(client)
    _mark_calendar_synced(
        client,
        CalendarSource.GOOGLE,
        GOOGLE_ACCOUNT_GENERATION,
    )
    session.add(
        CalendarEventMirror(
            external_id="old-account-event",
            calendar_source=CalendarSource.GOOGLE,
            connection_generation=GOOGLE_ACCOUNT_GENERATION,
            summary="Old account",
            start_at=_dt(10),
            end_at=_dt(11),
        )
    )
    session.commit()
    _connect_google(
        client,
        generation=RECONNECTED_GOOGLE_ACCOUNT_GENERATION,
    )

    response = client.get(
        "/v1/schedule/events",
        params={
            "start": "2026-07-06T00:00:00Z",
            "end": "2026-07-07T00:00:00Z",
            "calendar_source": "google",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["detail"] == {"reason_codes": ["calendar_account_not_synced"]}


def test_list_events_discards_count_and_rows_when_reconnect_races_read(
    client,
    session,
    monkeypatch,
):
    _connect_google(client)
    _mark_calendar_synced(
        client,
        CalendarSource.GOOGLE,
        GOOGLE_ACCOUNT_GENERATION,
    )
    session.add(
        CalendarEventMirror(
            external_id="must-not-leak",
            calendar_source=CalendarSource.GOOGLE,
            connection_generation=GOOGLE_ACCOUNT_GENERATION,
            summary="Old account",
            start_at=_dt(10),
            end_at=_dt(11),
        )
    )
    session.commit()
    original_paginate = schedule_api.paginate
    calls = 0

    def reconnect_after_page(*args, **kwargs):
        nonlocal calls
        result = original_paginate(*args, **kwargs)
        calls += 1
        if calls == 1:
            _connect_google(
                client,
                generation=RECONNECTED_GOOGLE_ACCOUNT_GENERATION,
            )
        return result

    monkeypatch.setattr(schedule_api, "paginate", reconnect_after_page)

    response = client.get(
        "/v1/schedule/events",
        params={
            "start": "2026-07-06T00:00:00Z",
            "end": "2026-07-07T00:00:00Z",
            "calendar_source": "google",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["detail"] == {"reason_codes": ["calendar_account_not_synced"]}
    assert calls == 1


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
        json={"resolution_token": token, "surface": "ios_notification"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    session.expire_all()
    stored = session.get(ScheduleProposal, proposal.id)
    assert stored.status == ProposalStatus.ACCEPTED
    assert stored.decided_at is not None
    assert stored.decision_surface == "ios_notification"

    again = client.post(
        f"/v1/schedule/proposals/{proposal.id}/accept",
        json={"resolution_token": token},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_transition"


def test_accept_invalidates_proposal_when_actual_sleep_changed(client, session):
    _connect_google(client)
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
            connection_generation=GOOGLE_ACCOUNT_GENERATION,
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
    assert response.json()["error"]["detail"] == {"proposal_status": "invalidated"}
    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.INVALIDATED


def test_invalidation_does_not_overwrite_a_resolved_proposal(session):
    proposal = _seed_proposal(session)
    proposal.status = ProposalStatus.ACCEPTED
    session.commit()

    with pytest.raises(ScheduleProposalResolutionError, match="not_proposed"):
        invalidate_schedule_proposal(session, proposal.id)

    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.ACCEPTED


def test_invalidation_does_not_consume_an_expired_proposal(session):
    proposal = _seed_proposal(session)
    expired_at = proposal.expires_at + timedelta(seconds=1)

    with pytest.raises(ScheduleProposalResolutionError, match="expired"):
        invalidate_schedule_proposal(session, proposal.id, now=expired_at)

    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.PROPOSED


def test_decline_proposal(client, session):
    proposal = _seed_proposal(session)
    token = _resolution_token(client, proposal, action="decline")

    declined = client.post(
        f"/v1/schedule/proposals/{proposal.id}/decline",
        json={"resolution_token": token},
    )

    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"


def test_shared_resolver_records_non_rest_surface(session):
    proposal, handle = _seed_proposal_with_handle(session)

    resolved = resolve_schedule_proposal(
        session,
        proposal.id,
        ProposalStatus.DECLINED,
        handle,
        HANDLE_SECRET,
        surface="telegram",
    )
    session.commit()

    assert resolved.decided_at is not None
    assert resolved.decision_surface == "telegram"


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
    expired_token = _resolution_token(client, proposal)

    missing = client.post(endpoint)
    assert missing.status_code == 422

    invalid = client.post(endpoint, json={"resolution_token": "wrong-token"})
    assert invalid.status_code == 403
    assert invalid.json()["error"]["code"] == "invalid_resolution_token"

    with freeze_time(proposal.expires_at + timedelta(seconds=1)):
        expired = client.post(endpoint, json={"resolution_token": expired_token})
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "proposal_expired"

    session.expire_all()
    assert session.get(ScheduleProposal, proposal.id).status is ProposalStatus.PROPOSED


def test_expired_proposal_is_hidden_from_pending_list_without_losing_direct_audit(
    client,
    session,
):
    proposal = _seed_proposal(session)
    proposal.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    listed = client.get(
        "/v1/schedule/proposals",
        params={"status": "proposed"},
    ).json()
    response = client.get(f"/v1/schedule/proposals/{proposal.id}")

    assert listed["data"] == []
    assert listed["pagination"]["total_count"] == 0
    assert response.status_code == 200
    assert response.json()["status"] == "proposed"
    assert response.json()["accept_resolution_token"] is None
    assert response.json()["decline_resolution_token"] is None


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
