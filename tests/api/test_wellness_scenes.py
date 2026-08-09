"""Bounded wellness scene presentation API."""

import os
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from healthmes.api.auth import viewer_token
from healthmes.api.dashboard import build_dashboard
from healthmes.api.wellness_scenes import (
    WellnessActionOut,
    WellnessCalendarEventOut,
    WellnessConfidenceOut,
    WellnessModuleOut,
    WellnessSceneOut,
    WellnessSceneRequest,
    WellnessVisualizationOut,
    _freshness,
    _intent,
)
from healthmes.app import create_app
from healthmes.calendars.state import FileSyncStateStore
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TaskSource,
    TriggerEvent,
)
from healthmes.store.session import get_engine

TOKEN = "wellness-scene-api-token"


def _seed_proposal(
    session,
    *,
    mirrored_task_id: uuid.UUID | None = None,
    duplicate_title: bool = False,
    decision_record_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    task = Task(
        title="Deep Work",
        goal_id=None,
        est_minutes=90,
        deadline=now + timedelta(days=1),
        status="scheduled",
        source=TaskSource.USER,
    )
    session.add(task)
    session.flush()
    proposal = ScheduleProposal(
        task_id=task.id,
        proposed_start=now + timedelta(hours=2),
        proposed_end=now + timedelta(hours=3, minutes=30),
        status=ProposalStatus.PROPOSED,
        decision_record_id=decision_record_id,
        healthmes_kind="schedule_change",
        reply_handle_digest=None,
        expires_at=now + timedelta(hours=1),
        decided_at=None,
        decision_surface=None,
        intake_calendar_source=None,
        intake_external_id=None,
        intake_revision=None,
    )
    session.add(proposal)
    session.flush()
    if duplicate_title and mirrored_task_id is None:
        duplicate = Task(
            title="Deep Work",
            goal_id=None,
            est_minutes=60,
            deadline=now + timedelta(days=1),
            status="scheduled",
            source=TaskSource.USER,
        )
        session.add(duplicate)
        session.flush()
        mirrored_task_id = duplicate.id
    if mirrored_task_id is not None or duplicate_title:
        session.add(
            CalendarEventMirror(
                external_id=f"event-{uuid.uuid4()}",
                calendar_source=CalendarSource.CALDAV,
                summary="Deep Work",
                start_at=now + timedelta(hours=5),
                end_at=now + timedelta(hours=6),
                is_agent_created=True,
                agent_task_id=mirrored_task_id,
                intake_task_id=None,
                intake_opted_out=False,
                healthmes_kind=None,
                healthmes_source=None,
                healthmes_source_key=None,
                observation_fingerprint=None,
                sleep_local_date=None,
                sleep_provider=None,
                sleep_duration_minutes=None,
                sleep_time_in_bed_minutes=None,
                etag=None,
                sync_token=None,
                organizer_self=True,
                has_attendees=False,
                is_recurring=False,
                event_type=None,
                is_all_day=False,
                is_locked=False,
                status="confirmed",
            )
        )
    session.commit()
    return task.id, proposal.id


def _seed_proactive_proposal(
    session,
    *,
    duplicate_title: bool = False,
    decision_kind: DecisionKind = DecisionKind.SCHEDULE_CHANGE,
) -> tuple[uuid.UUID, uuid.UUID]:
    decision = DecisionRecord(
        kind=decision_kind,
        tree={
            "id": "root",
            "type": "rule",
            "label": "capacity",
            "children": [],
        },
        summary="Stored proposal decision",
    )
    session.add(decision)
    session.flush()
    _, proposal_id = _seed_proposal(
        session,
        duplicate_title=duplicate_title,
        decision_record_id=decision.id,
    )
    return proposal_id, decision.id


def _set_provenance_time(
    session,
    proposal_id: uuid.UUID,
    decision_id: uuid.UUID,
    observed_at: datetime,
) -> None:
    proposal = session.get(ScheduleProposal, proposal_id)
    decision = session.get(DecisionRecord, decision_id)
    assert proposal is not None
    assert decision is not None
    proposal.created_at = observed_at
    decision.created_at = observed_at
    session.commit()


def _seed_energy(session, *, window_start: datetime, score: int = 54) -> None:
    window_start = window_start.replace(minute=0, second=0, microsecond=0)
    session.add(
        CognitiveEnergyEstimate(
            window_start=window_start,
            window_end=window_start + timedelta(hours=1),
            score=score,
            components={},
            inputs_snapshot=None,
        )
    )
    session.commit()


def _seed_current_calendar(client, settings, *sources: CalendarSource) -> None:
    updates = {}
    if CalendarSource.GOOGLE in sources:
        updates["google_calendar_enabled"] = True
    if CalendarSource.CALDAV in sources:
        updates["caldav_enabled"] = True
    client.app.state.settings = client.app.state.settings.model_copy(update=updates)
    store = FileSyncStateStore.for_data_dir(settings.data_dir)
    for source in sources:
        store.save(source, {"sync_token": f"current-{source.value}"})


@contextmanager
def _secured_client(settings):
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    with TestClient(create_app(secured)) as client:
        Base.metadata.create_all(get_engine())
        yield client


def test_intent_classifier_keeps_questions_bounded() -> None:
    assert _intent("오늘 왜 이렇게 피곤해?", "user") == "explain_fatigue"
    assert _intent("언제 집중 업무를 해야 해?", "user") == "find_focus_window"
    assert _intent("이번 주 일정 괜찮아?", "user") == "review_week_capacity"
    assert _intent("오늘 일정을 조정해줘", "user") == "reschedule_for_capacity"
    assert _intent("왜 이 일정을 옮겨야 해?", "user") == "reschedule_for_capacity"
    assert _intent("오늘 캘린더 보여줘", "user") == "view_calendar"
    assert _intent("ignored", "proactive") == "proactive_intervention"


def test_scene_endpoint_returns_trusted_visual_catalog(client) -> None:
    response = client.post(
        "/v1/wellness/scenes",
        json={"query": "오늘 왜 이렇게 피곤해?", "source": "user"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "1"
    assert payload["intent"] == "explain_fatigue"
    assert payload["freshness"] == "insufficient_data"
    assert [module["kind"] for module in payload["modules"]] == [
        "time_series",
        "capacity_bar",
    ]
    assert payload["modules"][0]["visualization"] is None
    assert "그래프에 표시됩니다" not in payload["modules"][0]["accessibility_summary"]
    assert payload["modules"][1]["visualization"] is None
    assert payload["actions"] == []


def test_scene_request_rejects_blank_and_oversized_queries() -> None:
    for value in ("", "   \n\t", "x" * 501):
        try:
            WellnessSceneRequest(query=value)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid query accepted")


def test_scene_timestamps_are_aware(client) -> None:
    response = client.post(
        "/v1/wellness/scenes",
        json={"query": "현재 상태", "source": "user"},
    )
    generated = datetime.fromisoformat(response.json()["generated_at"].replace("Z", "+00:00"))
    assert generated.tzinfo is not None
    assert generated.utcoffset() == UTC.utcoffset(generated)


def test_unrelated_pending_proposal_is_not_attached_to_focus_scene(client, session) -> None:
    _seed_proposal(session)

    response = client.post(
        "/v1/wellness/scenes",
        json={"query": "언제 집중 업무를 해야 해?", "source": "user"},
    )

    payload = response.json()
    calendar = next(module for module in payload["modules"] if module["kind"] == "calendar_canvas")
    events = (calendar["visualization"] or {}).get("events", [])
    assert all(event["status"] == "current" for event in events)
    assert payload["actions"] == []
    assert payload["severity"] == "supportive"


def test_nutrition_scene_fails_closed_without_confirmed_meal_evidence(client) -> None:
    response = client.post(
        "/v1/wellness/scenes",
        json={"query": "이 식사가 왜 오후 컨디션에 영향을 줬어?", "source": "user"},
    )

    payload = response.json()
    assert payload["intent"] == "review_nutrition_impact"
    assert payload["freshness"] == "insufficient_data"
    assert payload["confidence"]["level"] == "insufficient_data"
    assert payload["modules"][0]["kind"] == "nutrition_evidence"
    assert payload["modules"][0]["visualization"] is None
    assert payload["actions"] == []


def test_nutrition_summary_does_not_reuse_unrelated_alert_headline(client, session) -> None:
    unrelated = "오후 회의를 지금 취소하세요."
    now = datetime.now(UTC)
    session.add(
        TriggerEvent(
            fired_at=now,
            rule_id="unrelated-alert",
            payload={"summary": unrelated},
            alert_sent=True,
            dedup_key=f"unrelated:{uuid.uuid4()}",
        )
    )
    session.commit()

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "오늘 식사 기록이 컨디션과 관련 있어?", "source": "user"},
    ).json()

    assert payload["intent"] == "review_nutrition_impact"
    assert unrelated not in payload["summary"]
    assert "식사가 컨디션 변화의 원인이라고 단정하지 않습니다" in payload["summary"]


def test_proposal_preview_uses_exact_proposal_identity_not_duplicate_title(client, session) -> None:
    task_id, proposal_id = _seed_proposal(
        session,
        duplicate_title=True,
    )

    response = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    )

    payload = response.json()
    preview = payload["modules"][0]
    assert preview["kind"] == "proposal_preview"
    assert preview["visualization"] is None
    assert "생성인지 이동인지 단정하지 않습니다" in preview["summary"]
    assert next(item for item in preview["items"] if item["id"] == "proposal-id")["value"] == str(
        proposal_id
    )
    assert all(action["proposal_id"] for action in payload["actions"][:2])
    assert str(task_id) not in {item["value"] for item in preview["items"]}
    assert [module["kind"] for module in payload["modules"]] == [
        "proposal_preview",
        "calendar_canvas",
        "capacity_bar",
    ]
    calendar = payload["modules"][1]["visualization"]["events"]
    assert any(event["id"] == f"proposal:{proposal_id}" for event in calendar)


def test_schedule_scene_without_exact_proposal_id_fails_closed(client, session) -> None:
    _seed_proposal(session)

    response = client.post(
        "/v1/wellness/scenes",
        json={"query": "오늘 일정을 조정해줘", "source": "user"},
    )

    payload = response.json()
    assert payload["freshness"] == "insufficient_data"
    assert payload["modules"][0]["visualization"] is None
    assert payload["actions"] == []


def test_exact_proposal_is_not_lost_behind_dashboard_list_limit(
    client,
    session,
    settings,
) -> None:
    proposal_ids = [_seed_proposal(session)[1] for _ in range(3)]
    selected, _ = _seed_proactive_proposal(session)
    proposal_ids.append(selected)
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    response = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "이 일정 블록을 조정해줘",
            "source": "user",
            "proposal_id": str(selected),
        },
    )

    payload = response.json()
    assert payload["freshness"] == "current"
    assert payload["actions"]
    assert {action["proposal_id"] for action in payload["actions"]} == {str(selected)}


def test_truncated_calendar_disables_proposal_actions(
    client,
    session,
    monkeypatch,
) -> None:
    proposal_id, _ = _seed_proactive_proposal(session, duplicate_title=True)
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    monkeypatch.setattr("healthmes.api.dashboard.MAX_PLAN_EVENTS", 0)

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["actions"] == []
    assert "앞선 0건만 표시" in " ".join(payload["confidence"]["limitations"])
    assert payload["modules"][1]["kind"] == "calendar_canvas"


def test_stale_calendar_disables_proposal_actions(client, session) -> None:
    proposal_id, _ = _seed_proactive_proposal(session, duplicate_title=True)
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "stale"
    assert payload["actions"] == []


def test_future_calendar_sync_evidence_is_stale(
    client,
    session,
    settings,
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 8, 9, 14, 23, tzinfo=UTC)
    proposal_id, decision_id = _seed_proactive_proposal(session, duplicate_title=True)
    _set_provenance_time(session, proposal_id, decision_id, fixed_now)
    _seed_energy(session, window_start=fixed_now - timedelta(hours=1))
    monkeypatch.setattr(
        "healthmes.api.wellness_scenes.utc_now",
        lambda: fixed_now,
    )
    sync_store = FileSyncStateStore.for_data_dir(settings.data_dir)
    sync_store.save(CalendarSource.CALDAV, {"sync_token": "future"})
    sync_path = sync_store.path_for(CalendarSource.CALDAV)
    future = (fixed_now + timedelta(minutes=6)).timestamp()
    sync_path.touch()
    os.utime(sync_path, (future, future))

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "stale"
    assert payload["actions"] == []


def test_calendar_sync_at_future_skew_boundary_remains_current(
    client,
    session,
    settings,
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 8, 9, 14, 23, tzinfo=UTC)
    proposal_id, decision_id = _seed_proactive_proposal(session, duplicate_title=True)
    _set_provenance_time(session, proposal_id, decision_id, fixed_now)
    _seed_energy(session, window_start=fixed_now - timedelta(hours=1))
    monkeypatch.setattr(
        "healthmes.api.wellness_scenes.utc_now",
        lambda: fixed_now,
    )
    sync_store = FileSyncStateStore.for_data_dir(settings.data_dir)
    sync_store.save(CalendarSource.CALDAV, {"sync_token": "boundary"})
    sync_path = sync_store.path_for(CalendarSource.CALDAV)
    boundary = (fixed_now + timedelta(minutes=5)).timestamp()
    sync_path.touch()
    os.utime(sync_path, (boundary, boundary))

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "current"
    assert payload["confidence"]["level"] == "medium"
    assert payload["actions"]


def test_proposal_without_decision_provenance_has_no_actions(
    client,
    session,
    settings,
) -> None:
    _, proposal_id = _seed_proposal(session, duplicate_title=True)
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "current"
    assert payload["confidence"]["level"] == "low"
    assert "DecisionRecord가 없어" in " ".join(payload["confidence"]["limitations"])
    assert payload["actions"] == []


def test_proposal_with_wrong_decision_kind_has_no_actions(
    client,
    session,
    settings,
) -> None:
    proposal_id, _ = _seed_proactive_proposal(
        session,
        duplicate_title=True,
        decision_kind=DecisionKind.INSIGHT,
    )
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "current"
    assert payload["confidence"]["level"] == "low"
    assert "schedule change 판단 기록" in " ".join(payload["confidence"]["limitations"])
    assert payload["actions"] == []
    assert all(
        item["id"] != "proposal-reason"
        for item in payload["modules"][0]["items"]
    )


def test_free_form_health_evidence_tree_cannot_bypass_provenance(
    client,
    session,
    settings,
) -> None:
    proposal_id, decision_id = _seed_proactive_proposal(
        session,
        duplicate_title=True,
        decision_kind=DecisionKind.INSIGHT,
    )
    decision = session.get(DecisionRecord, decision_id)
    assert decision is not None
    decision.tree = {
        "id": "forged",
        "type": "input",
        "label": "health evidence",
        "detail": {
            "sleep_confidence": "high",
            "recovery_confidence": "high",
        },
    }
    session.commit()
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["confidence"]["level"] == "low"
    assert payload["actions"] == []
    assert all(
        item["id"] != "proposal-reason"
        for item in payload["modules"][0]["items"]
    )


def test_stale_decision_provenance_has_no_actions(
    client,
    session,
    settings,
) -> None:
    proposal_id, decision_id = _seed_proactive_proposal(
        session,
        duplicate_title=True,
    )
    decision = session.get(DecisionRecord, decision_id)
    assert decision is not None
    decision.created_at = decision.created_at - timedelta(minutes=16)
    session.commit()
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["confidence"]["level"] == "low"
    assert payload["actions"] == []


def test_future_decision_provenance_has_no_actions(
    client,
    session,
    settings,
) -> None:
    proposal_id, decision_id = _seed_proactive_proposal(
        session,
        duplicate_title=True,
    )
    decision = session.get(DecisionRecord, decision_id)
    assert decision is not None
    decision.created_at = datetime.now(UTC) + timedelta(minutes=6)
    session.commit()
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["confidence"]["level"] == "low"
    assert payload["actions"] == []


def test_missing_energy_disables_proposal_actions(
    client,
    session,
    settings,
) -> None:
    _, proposal_id = _seed_proposal(session, duplicate_title=True)
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "insufficient_data"
    assert payload["confidence"]["level"] == "insufficient_data"
    assert "현재 에너지 근거가 없어" in payload["confidence"]["limitations"][0]
    assert payload["actions"] == []


def test_low_confidence_scene_disables_proposal_actions(
    client,
    session,
    settings,
    monkeypatch,
) -> None:
    _, proposal_id = _seed_proposal(session, duplicate_title=True)
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)
    monkeypatch.setattr(
        "healthmes.api.wellness_scenes._confidence",
        lambda view, intent, proposal: WellnessConfidenceOut(
            level="low",
            coverage="forced low confidence",
        ),
    )

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "current"
    assert payload["confidence"]["level"] == "low"
    assert payload["actions"] == []


def test_unavailable_approval_secret_disables_proposal_actions(
    client,
    session,
    settings,
) -> None:
    _, proposal_id = _seed_proposal(session)
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"calendar_adjustment_secret": SecretStr("")}
    )

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "current"
    assert payload["actions"] == []


@pytest.mark.parametrize("terminal_state", ["expired", "declined"])
def test_expired_or_declined_proposal_has_no_scene_actions(client, session, terminal_state) -> None:
    _, proposal_id = _seed_proposal(session)
    proposal = session.get(ScheduleProposal, proposal_id)
    assert proposal is not None
    if terminal_state == "expired":
        proposal.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    else:
        proposal.status = ProposalStatus.DECLINED
    session.commit()

    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 조정해줘",
            "source": "user",
            "proposal_id": str(proposal_id),
        },
    ).json()

    assert payload["freshness"] == "insufficient_data"
    assert payload["actions"] == []
    assert payload["modules"][0]["kind"] == "proposal_preview"
    assert payload["modules"][0]["visualization"] is None


def test_scene_id_is_stable_for_same_query_and_evidence(client) -> None:
    request = {"query": "현재 상태", "source": "user"}

    first = client.post("/v1/wellness/scenes", json=request).json()
    second = client.post("/v1/wellness/scenes", json=request).json()

    assert first["id"] == second["id"]


def test_scene_id_changes_when_rendered_calendar_evidence_changes(client, session) -> None:
    request = {"query": "현재 상태", "source": "user"}
    first = client.post("/v1/wellness/scenes", json=request).json()
    now = datetime.now(UTC)
    session.add(
        CalendarEventMirror(
            external_id="new-calendar-evidence",
            calendar_source=CalendarSource.GOOGLE,
            summary="New evidence",
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
            is_agent_created=False,
            agent_task_id=None,
            intake_task_id=None,
            intake_opted_out=False,
            healthmes_kind=None,
            healthmes_source=None,
            healthmes_source_key=None,
            observation_fingerprint=None,
            sleep_local_date=None,
            sleep_provider=None,
            sleep_duration_minutes=None,
            sleep_time_in_bed_minutes=None,
            etag=None,
            sync_token=None,
            organizer_self=False,
            has_attendees=True,
            is_recurring=True,
            event_type=None,
            is_all_day=False,
            is_locked=True,
            status="tentative",
        )
    )
    session.commit()

    second = client.post("/v1/wellness/scenes", json=request).json()

    assert first["id"] != second["id"]


def test_calendar_scene_preserves_provider_metadata(client, session) -> None:
    _seed_proposal(session, duplicate_title=True)

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "언제 집중 업무를 해야 해?", "source": "user"},
    ).json()

    calendar = next(module for module in payload["modules"] if module["kind"] == "calendar_canvas")
    event = calendar["visualization"]["events"][0]
    assert event["provider"] == "caldav"
    assert event["organizer_self"] is True
    assert event["provider_status"] == "confirmed"


def test_calendar_view_intent_returns_only_connected_calendar_canvas(
    client,
    session,
    settings,
) -> None:
    _seed_proposal(session, duplicate_title=True)
    FileSyncStateStore.for_data_dir(settings.data_dir).save(
        CalendarSource.CALDAV,
        {"sync_token": "current"},
    )

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "오늘 캘린더 보여줘", "source": "user"},
    ).json()

    assert payload["intent"] == "view_calendar"
    assert payload["confidence"]["level"] == "high"
    assert [module["kind"] for module in payload["modules"]] == ["calendar_canvas"]
    assert payload["modules"][0]["title"] == "연결된 캘린더 일정"
    assert payload["actions"] == []


def test_calendar_without_successful_sync_evidence_is_stale(client, session) -> None:
    _seed_proposal(session, duplicate_title=True)

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "오늘 캘린더 보여줘", "source": "user"},
    ).json()

    assert payload["freshness"] == "stale"
    assert payload["confidence"]["level"] == "low"
    assert "최근 성공 sync" in " ".join(payload["confidence"]["limitations"])


def test_all_connected_calendar_providers_require_sync_evidence(
    client,
    session,
    settings,
) -> None:
    _seed_proposal(session, duplicate_title=True)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={
            "google_calendar_enabled": True,
            "caldav_enabled": True,
        }
    )
    FileSyncStateStore.for_data_dir(settings.data_dir).save(
        CalendarSource.GOOGLE,
        {"sync_token": "google-current"},
    )

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "오늘 캘린더 보여줘", "source": "user"},
    ).json()

    assert payload["freshness"] == "stale"
    assert payload["confidence"]["level"] == "low"


def test_week_capacity_requires_energy_evidence(client, session) -> None:
    _seed_proposal(session, duplicate_title=True)

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "이번 주 일정 괜찮아?", "source": "user"},
    ).json()

    assert payload["intent"] == "review_week_capacity"
    assert payload["freshness"] == "insufficient_data"
    assert payload["confidence"]["level"] == "insufficient_data"
    assert "현재 가용 에너지" in payload["confidence"]["limitations"][0]
    assert [module["kind"] for module in payload["modules"]] == [
        "comparison_bar",
        "calendar_canvas",
    ]
    assert sum(module["visualization"] is not None for module in payload["modules"]) <= 2


@pytest.mark.parametrize(
    "intent",
    [
        "review_week_capacity",
        "reschedule_for_capacity",
        "proactive_intervention",
    ],
)
def test_stale_energy_marks_capacity_decisions_stale(
    client,
    session,
    intent,
) -> None:
    now = datetime(2026, 8, 9, 14, 23, tzinfo=UTC)
    view = build_dashboard(session, client.app.state.settings, now)
    view = replace(
        view,
        energy=view.energy.model_copy(update={"score": 54, "confidence": "low"}),
        calendar_sources=("caldav",),
        calendar_sync_observed_at={"caldav": now},
    )
    confidence = WellnessConfidenceOut(level="low", coverage="test")

    assert _freshness(view, intent, confidence) == "stale"


def test_stale_energy_is_freshness_not_analysis_confidence(client, session, monkeypatch) -> None:
    fixed_now = datetime(2026, 8, 9, 14, 23, tzinfo=UTC)
    monkeypatch.setattr(
        "healthmes.api.wellness_scenes.utc_now",
        lambda: fixed_now,
    )
    _seed_energy(
        session,
        window_start=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
    )

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "오늘 왜 이렇게 피곤해?", "source": "user"},
    ).json()

    assert payload["freshness"] == "stale"
    assert payload["confidence"]["level"] == "low"
    assert "stale" not in payload["confidence"].values()


def test_focus_scene_requires_calendar_evidence(client, session, monkeypatch) -> None:
    fixed_now = datetime(2026, 8, 9, 14, 23, tzinfo=UTC)
    monkeypatch.setattr(
        "healthmes.api.wellness_scenes.utc_now",
        lambda: fixed_now,
    )
    _seed_energy(
        session,
        window_start=datetime(2026, 8, 9, 14, 0, tzinfo=UTC),
    )

    payload = client.post(
        "/v1/wellness/scenes",
        json={"query": "언제 집중 업무를 해야 해?", "source": "user"},
    ).json()

    assert payload["intent"] == "find_focus_window"
    assert payload["freshness"] == "insufficient_data"
    assert payload["confidence"]["level"] == "insufficient_data"
    assert "캘린더 일정이 없습니다" in payload["confidence"]["limitations"][0]


def test_scene_response_is_private_and_not_cached(client) -> None:
    response = client.post(
        "/v1/wellness/scenes",
        json={"query": "현재 상태", "source": "user"},
    )

    assert response.headers["cache-control"] == "private, no-store"


def test_korean_coordinate_command_selects_reschedule_intent(client) -> None:
    payload = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "오늘 일정을 내 회복 상태에 맞춰 조율해줘",
            "source": "user",
        },
    ).json()

    assert payload["intent"] == "reschedule_for_capacity"
    assert payload["title"] == "현재 몸 상태에 맞춘 일정 조율"


def test_proactive_scene_requires_proposal_and_decision_identity(client) -> None:
    for body in (
        {"query": "late wake trigger", "source": "proactive"},
        {
            "query": "late wake trigger",
            "source": "proactive",
            "proposal_id": str(uuid.uuid4()),
        },
    ):
        response = client.post("/v1/wellness/scenes", json=body)
        assert response.status_code == 422


def test_proactive_scene_rejects_mismatched_decision_identity(client, session) -> None:
    proposal_id, _ = _seed_proactive_proposal(session)

    response = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "late wake trigger",
            "source": "proactive",
            "proposal_id": str(proposal_id),
            "decision_record_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 422


def test_proactive_scene_accepts_exact_active_provenance(
    client,
    session,
    settings,
) -> None:
    proposal_id, decision_id = _seed_proactive_proposal(session)
    _seed_energy(session, window_start=datetime.now(UTC) - timedelta(hours=1))
    _seed_current_calendar(client, settings, CalendarSource.CALDAV)

    response = client.post(
        "/v1/wellness/scenes",
        json={
            "query": "late wake trigger",
            "source": "proactive",
            "proposal_id": str(proposal_id),
            "decision_record_id": str(decision_id),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "proactive_intervention"
    assert payload["freshness"] == "current"
    assert {action["proposal_id"] for action in payload["actions"]} == {str(proposal_id)}
    preview = payload["modules"][0]
    assert next(item for item in preview["items"] if item["id"] == "proposal-reason")[
        "value"
    ] == "Stored proposal decision"
    assert payload["modules"][1]["kind"] == "calendar_canvas"


def test_scene_schema_rejects_unknown_kinds_ranges_and_mismatches() -> None:
    common = {
        "id": "module",
        "title": "title",
        "summary": "summary",
        "accessibility_summary": "summary",
    }

    with pytest.raises(ValidationError, match="kind"):
        WellnessModuleOut(kind="unknown", **common)
    with pytest.raises(ValidationError, match="minimum"):
        WellnessVisualizationOut(
            kind="capacity_bar",
            minimum=10,
            maximum=10,
        )
    with pytest.raises(ValidationError, match="must match"):
        WellnessModuleOut(
            kind="capacity_bar",
            visualization=WellnessVisualizationOut(kind="time_series"),
            **common,
        )
    with pytest.raises(ValidationError, match="starts_at"):
        WellnessCalendarEventOut(
            id="event",
            title="event",
            starts_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
            ends_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
            provider="google",
            is_healthmes_managed=False,
        )


def test_scene_schema_rejects_duplicate_module_and_action_ids() -> None:
    module = WellnessModuleOut(
        id="duplicate",
        kind="nutrition_evidence",
        title="title",
        summary="summary",
        accessibility_summary="summary",
    )
    action = WellnessActionOut(
        id="duplicate",
        kind="refresh",
        label="refresh",
    )
    common = {
        "id": "scene",
        "intent": "wellness_overview",
        "timezone": "UTC",
        "lens": "now",
        "title": "title",
        "summary": "summary",
        "severity": "neutral",
        "freshness": "current",
        "confidence": WellnessConfidenceOut(
            level="low",
            coverage="coverage",
        ),
        "generated_at": datetime(2026, 8, 9, tzinfo=UTC),
    }

    with pytest.raises(ValidationError, match="module ids"):
        WellnessSceneOut(modules=[module, module], **common)
    with pytest.raises(ValidationError, match="action ids"):
        WellnessSceneOut(
            modules=[module],
            actions=[action, action],
            **common,
        )


def test_scene_endpoint_requires_bearer_not_viewer_query_token(settings) -> None:
    body = {"query": "현재 상태", "source": "user"}
    with _secured_client(settings) as client:
        missing = client.post("/v1/wellness/scenes", json=body)
        wrong = client.post(
            "/v1/wellness/scenes",
            json=body,
            headers={"Authorization": "Bearer wrong"},
        )
        viewer = client.post(
            "/v1/wellness/scenes",
            params={"token": viewer_token(TOKEN)},
            json=body,
        )
        allowed = client.post(
            "/v1/wellness/scenes",
            json=body,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert viewer.status_code == 401
    assert allowed.status_code == 200
    assert missing.headers["cache-control"] == "private, no-store"


def test_scene_validation_errors_are_private_and_not_cached(settings) -> None:
    with _secured_client(settings) as client:
        response = client.post(
            "/v1/wellness/scenes",
            json={"query": "", "source": "user"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
