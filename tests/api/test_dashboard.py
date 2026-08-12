"""Unified dashboard and friendly human-viewer authentication."""

import os
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.api.dashboard import _local_window, build_dashboard
from healthmes.app import create_app
from healthmes.calendars.state import FileSyncStateStore
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    Insight,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TaskSource,
    WeeklyGoal,
)
from healthmes.store.session import get_engine

TOKEN = "dashboard-api-token"


@contextmanager
def _secured_client(settings):
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    with TestClient(create_app(secured)) as client:
        Base.metadata.create_all(get_engine())
        yield client


def _seed_dashboard(session) -> uuid.UUID:
    now = datetime.now(UTC)
    monday = now.date() - timedelta(days=now.weekday())
    goal = WeeklyGoal(
        week_start=monday,
        title="Apple 앱 Live QA",
        priority=8,
        status="active",
    )
    session.add(goal)
    session.flush()
    task = Task(
        title="Deep Work",
        goal_id=goal.id,
        est_minutes=90,
        deadline=now + timedelta(days=1),
        energy_demand=EnergyDemand.HIGH,
        status="scheduled",
        source=TaskSource.USER,
    )
    decision = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        summary="Deep Work를 회복 가능한 시간으로 옮깁니다.",
        tree={"id": "root", "type": "rule", "label": "sleep deficit", "children": []},
        llm_model=None,
        tokens=None,
    )
    session.add_all([task, decision])
    session.flush()
    session.add_all(
        [
            ScheduleProposal(
                task_id=task.id,
                proposed_start=now + timedelta(hours=2),
                proposed_end=now + timedelta(hours=3, minutes=30),
                status=ProposalStatus.PROPOSED,
                decision_record_id=decision.id,
                healthmes_kind="schedule_change",
                reply_handle_digest=None,
                expires_at=now + timedelta(hours=1),
                decided_at=None,
                decision_surface=None,
                intake_calendar_source=None,
                intake_external_id=None,
                intake_revision=None,
            ),
            CalendarEventMirror(
                external_id="calendar-deep-work",
                calendar_source=CalendarSource.CALDAV,
                summary="Deep Work",
                start_at=now + timedelta(hours=5),
                end_at=now + timedelta(hours=6, minutes=30),
                is_agent_created=True,
                agent_task_id=task.id,
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
            ),
            CalendarEventMirror(
                external_id="google-team-sync",
                calendar_source=CalendarSource.GOOGLE,
                summary="Team sync",
                start_at=now + timedelta(hours=1),
                end_at=now + timedelta(hours=1, minutes=30),
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
                organizer_self=True,
                has_attendees=False,
                is_recurring=False,
                event_type=None,
                is_all_day=False,
                is_locked=False,
                status="confirmed",
            ),
            CognitiveEnergyEstimate(
                window_start=now.replace(minute=0, second=0, microsecond=0),
                window_end=now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
                score=54,
                components={},
                inputs_snapshot=None,
            ),
            Insight(
                period="7d",
                kind="focus",
                statement="짧은 수면 뒤 오후 집중 시간이 흔들렸습니다.",
                evidence={},
                confidence=0.8,
            ),
        ]
    )
    session.commit()
    return decision.id


def _calendar_event(
    *,
    external_id: str,
    summary: str,
    start_at: datetime,
    end_at: datetime,
) -> CalendarEventMirror:
    return CalendarEventMirror(
        external_id=external_id,
        calendar_source=CalendarSource.GOOGLE,
        summary=summary,
        start_at=start_at,
        end_at=end_at,
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
        organizer_self=True,
        has_attendees=False,
        is_recurring=False,
        event_type=None,
        is_all_day=False,
        is_locked=False,
        status="confirmed",
    )


def test_dashboard_renders_channel_workspace_with_existing_data(client, session) -> None:
    decision_id = _seed_dashboard(session)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    html = response.text
    assert "data-control-shell" in html
    assert "data-workspace-shell" in html
    assert "data-workspace-sidebar" in html
    assert html.count("<main") == 1
    for channel in ("overview", "calendar", "insights", "decisions", "agent"):
        assert f'data-channel-id="{channel}"' in html
        assert f'data-channel-panel="{channel}"' in html
    assert "data-local-categories" in html
    assert "data-custom-panels" in html
    assert "data-thread-panel" in html
    assert "data-category-dialog" in html
    assert "data-channel-dialog" in html
    assert "healthmes.workspace.web.v1" in html
    assert "window.localStorage" in html
    assert "이 브라우저에만 저장" in html
    assert "fetch(" not in html
    for primitive in (
        "capacity_bar",
        "calendar_canvas",
        "proposal_preview",
        "comparison_bar",
        "weekly_energy_trajectory",
        "weekly_plan",
        "calendar_ownership",
        "outcome_summary",
        "nutrition_summary",
        "decision_history",
        "insight_list",
        "learning_loop",
    ):
        assert f'data-ui-primitive="{primitive}"' in html
    assert "data-duration-minutes=" in html
    assert "--duration-minutes:" in html
    assert html.index("Team sync") < html.index("Deep Work")
    assert "Apple 앱 Live QA" in html
    assert "Deep Work" in html
    assert "Team sync" in html
    assert "짧은 수면 뒤 오후 집중 시간이 흔들렸습니다." in html
    assert f"/decisions/{decision_id}" in html
    assert 'data-calendar-provider="google"' in html
    assert 'data-calendar-provider="caldav"' in html
    assert 'data-calendar-provider="healthmes"' in html
    assert 'data-provider-status="confirmed"' in html
    assert 'data-organizer-self="true"' in html
    assert "Google Calendar" in html
    assert "Apple / iCloud Calendar" in html
    assert "HealthMes 제안" in html
    assert "요구 에너지 high" in html
    assert "내가 주최" in html
    assert "상태 confirmed" in html
    assert 'data-ui-primitive="energy_curve"' not in html
    assert "데이터 없음" in html
    assert "데이터 최신성 현재 시간대" in " ".join(html.split())
    assert "현재 계약" in html
    assert "제안 블록" in html
    assert "기존 캘린더 배치를 확인할 수 없음" not in html
    assert "이유" in html
    assert "결과" in html
    assert "연결된 캘린더 쓰기 절차로 전달됩니다." in html
    assert "<details" in html
    assert '<details class="primitive advanced" id="advanced">' in html
    assert "Advanced · 연결, 원시 데이터, 진단" in html
    assert 'role="progressbar"' in html
    assert "응답 기한 " in html
    assert "Yes/No는 iPhone, Mac 또는 Apple Watch에서 실행합니다." in html
    assert "Google Calendar" in html
    assert "iCloud 캘린더 (CalDAV)" in html
    assert "@media (max-width: 760px)" in html
    assert "@media (max-width: 540px)" in html
    assert "몸의 상태가 오늘 계획을 어떻게 바꿔야 하는지 봅니다." not in html
    assert "현재 몸 상태가 오늘 일정에 미치는 영향" not in html


def test_dashboard_calendar_filters_proposals_to_the_visible_week(session, settings) -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    task = Task(title="Far future review", source=TaskSource.USER)
    session.add(task)
    session.flush()
    session.add(
        ScheduleProposal(
            task_id=task.id,
            proposed_start=now + timedelta(days=8),
            proposed_end=now + timedelta(days=8, hours=1),
            status=ProposalStatus.PROPOSED,
            expires_at=now + timedelta(hours=1),
        )
    )
    session.commit()

    dashboard = build_dashboard(session, settings, now)

    assert [proposal.task_title for proposal in dashboard.pending_proposals] == [
        "Far future review"
    ]
    assert dashboard.calendar_proposals == []


def test_dashboard_calendar_gap_uses_latest_overlapping_end(
    client,
    session,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    monkeypatch.setattr("healthmes.api.dashboard.utc_now", lambda: now)
    session.add_all(
        [
            _calendar_event(
                external_id="long-block",
                summary="Long block",
                start_at=now + timedelta(hours=1),
                end_at=now + timedelta(hours=5),
            ),
            _calendar_event(
                external_id="nested-block",
                summary="Nested block",
                start_at=now + timedelta(hours=2),
                end_at=now + timedelta(hours=3),
            ),
            _calendar_event(
                external_id="after-nested",
                summary="After nested",
                start_at=now + timedelta(hours=4),
                end_at=now + timedelta(hours=4, minutes=30),
            ),
        ]
    )
    session.commit()

    html = client.get("/dashboard").text
    calendar_html = html.split('data-channel-panel="calendar"', 1)[1]
    event_tag = calendar_html[: calendar_html.index("After nested")].rsplit(
        '<li class="calendar-event',
        1,
    )[1].split(">", 1)[0]

    assert 'data-gap-minutes="0"' in event_tag


def test_dashboard_renders_proposal_preview_without_mirrored_events(client, session) -> None:
    now = datetime.now(UTC)
    task = Task(
        title="Recovery walk",
        goal_id=None,
        est_minutes=30,
        deadline=now + timedelta(days=1),
        status="todo",
        source=TaskSource.USER,
    )
    session.add(task)
    session.flush()
    proposal = ScheduleProposal(
        task_id=task.id,
        proposed_start=now + timedelta(hours=2),
        proposed_end=now + timedelta(hours=2, minutes=30),
        status=ProposalStatus.PROPOSED,
        decision_record_id=None,
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
    session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Recovery walk 블록 후보" in response.text
    assert "변경 유형과 원본 event identity 미제공" in response.text
    assert "data-proposal-preview" in response.text
    assert f'data-proposal-id="{proposal.id}"' in response.text


def test_dashboard_reports_calendar_truncation(client, session) -> None:
    now = datetime.now(UTC)
    for index in range(101):
        session.add(
            CalendarEventMirror(
                external_id=f"calendar-{index}",
                calendar_source=CalendarSource.GOOGLE,
                summary=f"Event {index}",
                start_at=now + timedelta(minutes=index),
                end_at=now + timedelta(minutes=index + 30),
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

    view = build_dashboard(session, client.app.state.settings, now)

    assert len(view.plan_events) == 100
    assert view.plan_events_total == 101
    assert view.plan_events_truncated is True
    response = client.get("/dashboard")
    assert "캘린더 101건 중 시간순으로" in response.text
    assert "100건을 표시합니다." in response.text


def test_dashboard_reports_total_pending_proposals_beyond_preview_limit(client, session) -> None:
    now = datetime.now(UTC)
    for index in range(4):
        task = Task(
            title=f"Pending {index}",
            goal_id=None,
            est_minutes=30,
            deadline=now + timedelta(days=1),
            status="todo",
            source=TaskSource.USER,
        )
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=now + timedelta(hours=index + 1),
                proposed_end=now + timedelta(hours=index + 1, minutes=30),
                status=ProposalStatus.PROPOSED,
                decision_record_id=None,
                healthmes_kind="schedule_change",
                reply_handle_digest=None,
                expires_at=now + timedelta(hours=1),
                decided_at=None,
                decision_surface=None,
                intake_calendar_source=None,
                intake_external_id=None,
                intake_revision=None,
            )
        )
    session.commit()

    view = build_dashboard(session, client.app.state.settings, now)
    response = client.get("/dashboard")

    assert len(view.pending_proposals) == 3
    assert view.pending_proposals_total == 4
    assert view.pending_proposals_truncated is True
    assert "4개 대기" in response.text
    assert "총 4개 중" in response.text
    assert "3개를 시간순으로 불러왔고" in response.text


def test_dashboard_calendar_keeps_all_visible_pending_proposals(session, settings) -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    for index in range(4):
        task = Task(title=f"Visible proposal {index}", source=TaskSource.USER)
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=now + timedelta(hours=index + 1),
                proposed_end=now + timedelta(hours=index + 2),
                status=ProposalStatus.PROPOSED,
                expires_at=now + timedelta(days=1),
            )
        )
    session.commit()

    view = build_dashboard(session, settings, now)

    assert len(view.pending_proposals) == 3
    assert view.pending_proposals_truncated is True
    assert [proposal.task_title for proposal in view.calendar_proposals] == [
        "Visible proposal 0",
        "Visible proposal 1",
        "Visible proposal 2",
        "Visible proposal 3",
    ]
    assert view.calendar_proposals_total == 4
    assert view.calendar_proposals_truncated is False


def test_dashboard_calendar_caps_visible_pending_proposals(session, settings) -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    for index in range(101):
        task = Task(title=f"Visible proposal {index:03d}", source=TaskSource.USER)
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=now + timedelta(minutes=index),
                proposed_end=now + timedelta(minutes=index + 30),
                status=ProposalStatus.PROPOSED,
                expires_at=now + timedelta(days=1),
            )
        )
    session.commit()

    view = build_dashboard(session, settings, now)

    assert len(view.calendar_proposals) == 100
    assert view.calendar_proposals_total == 101
    assert view.calendar_proposals_truncated is True


def test_dashboard_all_day_event_uses_fixed_readable_height(
    client,
    session,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    monkeypatch.setattr("healthmes.api.dashboard.utc_now", lambda: now)
    event = _calendar_event(
        external_id="all-day",
        summary="Recovery day",
        start_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
    )
    event.is_all_day = True
    session.add(event)
    session.commit()

    html = client.get("/dashboard").text
    event_tag = html[: html.index("Recovery day")].rsplit(
        '<li class="calendar-event',
        1,
    )[1].split(">", 1)[0]

    assert 'data-all-day="true"' in event_tag
    assert "data-duration-minutes" not in event_tag


def test_dashboard_provider_legend_only_lists_rendered_providers(client, session) -> None:
    now = datetime.now(UTC)
    session.add(
        CalendarEventMirror(
            external_id="google-only",
            calendar_source=CalendarSource.GOOGLE,
            summary="Google only event",
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

    html = client.get("/dashboard").text

    assert '<span class="provider-key provider-google">' in html
    assert '<span class="provider-key provider-caldav">' not in html
    assert '<span class="provider-key provider-healthmes">' not in html
    assert "연결된 캘린더 일정" in html


def test_dashboard_exposes_calendar_sync_freshness(client, session, settings) -> None:
    now = datetime.now(UTC)
    session.add(
        CalendarEventMirror(
            external_id="google-freshness",
            calendar_source=CalendarSource.GOOGLE,
            summary="Freshness check",
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

    unconfirmed = client.get("/dashboard").text
    assert "Google sync 미확인" in " ".join(unconfirmed.split())

    FileSyncStateStore.for_data_dir(settings.data_dir).save(
        CalendarSource.GOOGLE,
        {"sync_token": "current"},
    )
    confirmed = client.get("/dashboard").text
    normalized = " ".join(confirmed.split())
    assert "Google sync 미확인" not in normalized
    assert "Google sync" in normalized


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(minutes=5), "Google sync"),
        (timedelta(minutes=5, seconds=1), "Google sync 시간 확인 필요"),
        (-timedelta(minutes=30), "Google sync"),
        (-timedelta(minutes=30, seconds=1), "Google sync 오래됨"),
    ],
)
def test_dashboard_uses_shared_calendar_sync_boundaries(
    client,
    session,
    settings,
    monkeypatch,
    offset,
    expected,
) -> None:
    fixed_now = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("healthmes.api.dashboard.utc_now", lambda: fixed_now)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"google_calendar_enabled": True}
    )
    store = FileSyncStateStore.for_data_dir(settings.data_dir)
    store.save(CalendarSource.GOOGLE, {"sync_token": "boundary"})
    path = store.path_for(CalendarSource.GOOGLE)
    timestamp = (fixed_now + offset).timestamp()
    os.utime(path, (timestamp, timestamp))

    normalized = " ".join(client.get("/dashboard").text.split())

    assert expected in normalized


def test_dashboard_agent_canvas_is_browser_local_and_non_mutating(client) -> None:
    response = client.get("/dashboard")

    assert response.status_code == 200
    html = response.text
    assert 'id="command-dock"' in html
    assert "data-command-dock" in html
    assert "Agent canvas" in html
    assert "현재 웹 클라이언트는 HealthMes Agent 실행 API에 연결하지 않습니다." in html
    assert "서버나 Calendar에는 반영되지 않습니다." in html
    assert 'id="command-preview"' in html
    assert 'data-local-draft="agent"' in html
    assert 'data-action="submit-local-post"' in html
    assert "window.localStorage" in html
    assert "fetch(" not in html
    assert "<form" not in html


def test_dashboard_legacy_routes_return_same_control_surface(client) -> None:
    for path, channel in (
        ("/dashboard/plan", "calendar"),
        ("/dashboard/decisions", "decisions"),
        ("/dashboard/history", "insights"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "data-control-shell" in response.text
        assert f'data-channel-panel="{channel}"' in response.text
        assert f'#channel-{channel}"' in response.text
        assert "channelFromLocation" in response.text


def test_empty_dashboard_is_honest_and_useful(client) -> None:
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "오늘 상태 데이터가 아직 없습니다." in response.text
    assert "지금 결정할 제안이 없습니다." in response.text
    assert "이번 주 활성 목표가 없습니다." in response.text
    assert "아직 기록된 판단이 없습니다." in response.text
    assert "오늘 기록된 식사나 섭취가 없습니다." in response.text
    assert "임의의 일정 변경이나 건강 원인을 만들어 표시하지 않습니다." in response.text
    assert 'class="capacity-score is-missing"' in response.text
    assert 'style="--capacity-value: 0"' not in response.text


def test_dashboard_renders_main_nutrition_interaction(client) -> None:
    now = datetime.now(UTC)
    created = client.post(
        "/v1/intake-interactions",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "log_consumed",
            "modality": "text",
            "observed_at": now.isoformat(),
            "timezone": "UTC",
            "source": "ios-device",
            "source_text": "닭가슴살 샐러드를 먹었다",
            "items": [
                {
                    "name": "닭가슴살 샐러드",
                    "intake_type": "food",
                    "serving": {
                        "kind": "exact",
                        "unit": "serving",
                        "exact": 1,
                        "estimation_basis": "owner_statement",
                    },
                    "nutrients": [],
                    "confidence": "high",
                }
            ],
        },
    )
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    confirmed = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": now.isoformat(),
        },
    )
    assert confirmed.status_code == 201

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "오늘 섭취" in response.text
    assert "닭가슴살 샐러드" in response.text
    assert "1건" in response.text


def test_dashboard_confirmed_count_is_not_limited_to_latest_five(client) -> None:
    now = datetime.now(UTC)
    for index in range(7):
        created = client.post(
            "/v1/intake-interactions",
            json={
                "operation_id": str(uuid.uuid4()),
                "intent": "log_consumed",
                "modality": "text",
                "observed_at": (now - timedelta(minutes=index)).isoformat(),
                "timezone": "UTC",
                "source": "ios-device",
                "source_text": f"확인된 식사 {index}",
                "items": [
                    {
                        "name": f"확인된 식사 {index}",
                        "intake_type": "food",
                        "serving": {
                            "kind": "exact",
                            "unit": "serving",
                            "exact": 1,
                            "estimation_basis": "owner_statement",
                        },
                        "nutrients": [],
                        "confidence": "high",
                    }
                ],
            },
        )
        assert created.status_code == 201
        interaction_id = created.json()["interaction_id"]
        outcome = client.post(
            f"/v1/intake-interactions/{interaction_id}/outcomes",
            json={
                "operation_id": str(uuid.uuid4()),
                "status": "consumed",
                "source": "ios-device",
                "consumed_at": (now - timedelta(minutes=index)).isoformat(),
            },
        )
        assert outcome.status_code == 201

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "확인 완료 7건" in response.text


def test_secured_dashboard_uses_friendly_unlock_and_exact_return(settings) -> None:
    with _secured_client(settings) as client:
        locked = client.get("/decisions/00000000-0000-0000-0000-000000000000?kind=alert")
        assert locked.status_code == 401
        assert locked.headers["content-type"].startswith("text/html")
        assert "HealthMes를 잠금 해제하세요" in locked.text
        assert 'name="next"' in locked.text
        assert "/decisions/00000000-0000-0000-0000-000000000000?kind=alert" in locked.text
        assert TOKEN not in locked.text
        assert viewer_token(TOKEN) not in locked.text

        wrong_query = client.get("/dashboard", params={"token": TOKEN})
        assert wrong_query.status_code == 401
        assert TOKEN not in wrong_query.text
        assert viewer_token(TOKEN) not in wrong_query.text

        unlocked = client.post(
            "/unlock",
            data={
                "viewer_token": viewer_token(TOKEN),
                "next": "/decisions/00000000-0000-0000-0000-000000000000?kind=alert",
            },
            follow_redirects=False,
        )
        assert unlocked.status_code == 303
        assert unlocked.headers["location"] == (
            "/decisions/00000000-0000-0000-0000-000000000000"
            f"?kind=alert&token={viewer_token(TOKEN)}"
        )
        assert TOKEN not in unlocked.headers["location"]


def test_unlock_rejects_wrong_token_and_external_redirect(settings) -> None:
    with _secured_client(settings) as client:
        rejected = client.post(
            "/unlock",
            data={"viewer_token": "wrong", "next": "https://attacker.test/steal"},
        )
        assert rejected.status_code == 401
        assert "viewer key가 올바르지 않습니다." in rejected.text
        assert 'value="/dashboard"' in rejected.text


def test_unlock_rejects_oversized_and_invalid_utf8(settings) -> None:
    with _secured_client(settings) as client:
        oversized = client.post(
            "/unlock",
            content=b"viewer_token=" + b"x" * (16 * 1024),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        malformed = client.post(
            "/unlock",
            content=b"viewer_token=\xff",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert oversized.status_code == 413
    assert "요청이 너무 큽니다." in oversized.text
    assert malformed.status_code == 400
    assert "요청 형식을 읽을 수 없습니다." in malformed.text


def test_api_and_json_surfaces_keep_json_401(settings) -> None:
    with _secured_client(settings) as client:
        api = client.get("/v1/tasks")
        report_json = client.get("/reports/weekly.json")

    assert api.status_code == 401
    assert api.json()["error"]["code"] == "unauthorized"
    assert report_json.status_code == 401
    assert report_json.json()["error"]["code"] == "unauthorized"


def test_viewer_token_does_not_authorize_similarly_named_routes(settings) -> None:
    with _secured_client(settings) as client:
        response = client.get(
            "/dashboard-evil",
            params={"token": viewer_token(TOKEN)},
        )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_dashboard_links_preserve_reverse_proxy_base_path(settings) -> None:
    proxied = settings.model_copy(update={"public_base_url": "https://example.test/healthmes"})
    with TestClient(create_app(proxied)) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert (
        'href="https://example.test/healthmes/dashboard#channel-overview"'
        in response.text
    )
    assert (
        'href="https://example.test/healthmes/dashboard/plan#channel-calendar"'
        in response.text
    )
    assert (
        'href="https://example.test/healthmes/dashboard/history#channel-insights"'
        in response.text
    )
    assert 'href="https://example.test/healthmes/connect"' in response.text
    assert 'href="https://example.test/healthmes/sleep"' in response.text
    assert 'href="https://example.test/healthmes/storage"' in response.text


def test_unlock_form_preserves_reverse_proxy_base_path(settings) -> None:
    proxied = settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "public_base_url": "https://example.test/healthmes",
        }
    )
    with TestClient(create_app(proxied)) as client:
        response = client.get("/dashboard")

    assert response.status_code == 401
    assert 'action="https://example.test/healthmes/unlock"' in response.text


def test_authenticated_dashboard_decision_links_are_read_only(settings) -> None:
    with _secured_client(settings) as client:
        # Create a record through the app lifespan engine.
        from healthmes.store.session import session_scope

        with session_scope() as session:
            decision = DecisionRecord(
                kind=DecisionKind.INSIGHT,
                summary="주간 회복 패턴을 확인합니다.",
                tree={},
                llm_model=None,
                tokens=None,
            )
            session.add(decision)
            session.commit()
            decision_id = decision.id

        response = client.get("/dashboard", params={"token": viewer_token(TOKEN)})

    assert response.status_code == 200
    expected = f"http://healthmes.test:8100/decisions/{decision_id}?token={viewer_token(TOKEN)}"
    assert expected.replace("&", "&amp;") in response.text
    channel_link = (
        f"/dashboard/plan?token={viewer_token(TOKEN)}#channel-calendar"
    )
    assert f'href="{channel_link}"' in response.text
    assert TOKEN not in response.text


def test_dashboard_hides_untrusted_proposal_reason(client, session, settings) -> None:
    decision_id = _seed_dashboard(session)
    decision = session.get(DecisionRecord, decision_id)
    assert decision is not None
    decision.kind = DecisionKind.INSIGHT
    decision.summary = "검증되지 않은 자유형 이유"
    session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "<dd>검증되지 않은 자유형 이유</dd>" not in response.text
    assert "연결된 판단 기록을 검증하지 못해 이유를 표시하지 않습니다." in response.text


def test_dashboard_normalizes_sqlite_naive_datetimes(session, settings) -> None:
    now = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    seoul = settings.model_copy(update={"timezone": "Asia/Seoul"})
    session.add(
        CalendarEventMirror(
            external_id="naive-sqlite-event",
            calendar_source=CalendarSource.CALDAV,
            summary="Midnight planning",
            start_at=datetime(2026, 8, 6, 16, 0),
            end_at=datetime(2026, 8, 6, 17, 0),
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
    session.expire_all()

    view = build_dashboard(session, seoul, now)

    assert view.local_today == date(2026, 8, 7)
    assert view.plan_events[0].starts_at == datetime(2026, 8, 6, 16, 0, tzinfo=UTC)


def test_local_window_tracks_dst_calendar_days(settings) -> None:
    new_york = settings.model_copy(update={"timezone": "America/New_York"})
    now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)

    local_today, start, end = _local_window(now, new_york, days=7)

    assert local_today == date(2026, 11, 1)
    assert start == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 8, 5, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=169)
