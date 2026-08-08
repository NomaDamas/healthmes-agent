"""Unified dashboard and friendly human-viewer authentication."""

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.api.dashboard import _local_window, build_dashboard
from healthmes.app import create_app
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
                start_at=now + timedelta(hours=2),
                end_at=now + timedelta(hours=3, minutes=30),
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
            CognitiveEnergyEstimate(
                window_start=now.replace(minute=0, second=0, microsecond=0),
                window_end=now.replace(minute=0, second=0, microsecond=0)
                + timedelta(hours=1),
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


def test_dashboard_renders_single_wellness_control_canvas(client, session) -> None:
    decision_id = _seed_dashboard(session)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    html = response.text
    assert "data-control-shell" in html
    assert 'role="tablist"' in html
    assert 'data-lens-target="now"' in html
    assert 'data-lens-target="adjust"' in html
    assert 'data-lens-target="change"' in html
    assert ">지금</strong>" in html
    assert ">조율</strong>" in html
    assert ">변화</strong>" in html
    assert "dashboard-tabs" not in html
    for section in ("today", "plan", "decisions", "history"):
        assert f'id="{section}"' in html
    for primitive in (
        "wellness_state",
        "impact_flow",
        "nutrition_summary",
        "schedule_timeline",
        "decision_remote",
        "proposal",
        "goal_progress",
        "decision_history",
        "outcome_summary",
        "insight_list",
        "learning_loop",
    ):
        assert f'data-ui-primitive="{primitive}"' in html
    assert "Apple 앱 Live QA" in html
    assert "Deep Work" in html
    assert "짧은 수면 뒤 오후 집중 시간이 흔들렸습니다." in html
    assert f"/decisions/{decision_id}" in html
    assert "<details" in html
    assert '<details class="advanced" id="advanced">' in html
    assert "Advanced · 연결, 원시 데이터, 긴 기록" in html
    assert 'role="progressbar"' in html
    assert "응답 기한 " in html
    assert "Apple 앱에서 Yes 또는 No를 결정합니다." in html
    assert "Google Calendar" in html
    assert "iCloud 캘린더 (CalDAV)" in html
    assert "몸의 상태가 오늘 계획을 어떻게 바꿔야 하는지 봅니다." not in html
    assert "현재 몸 상태가 오늘 일정에 미치는 영향" not in html


def test_dashboard_command_dock_is_persistent_visual_and_read_only(client) -> None:
    response = client.get("/dashboard")

    assert response.status_code == 200
    html = response.text
    assert 'id="command-dock"' in html
    assert "data-command-dock" in html
    assert "HealthMes에 말하거나 입력하기" in html
    assert "음성·텍스트 명령과 Yes/No 실행은 iPhone 또는 Mac 앱에서 합니다." in html
    assert 'id="command-preview"' in html
    assert "readonly" in html
    assert html.count("disabled") >= 2
    assert "<form" not in html


def test_dashboard_legacy_routes_return_same_control_surface(client) -> None:
    for path, lens, fragment in (
        ("/dashboard/plan", "adjust", 'id="plan"'),
        ("/dashboard/decisions", "adjust", 'id="decisions"'),
        ("/dashboard/history", "change", 'id="history"'),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "data-control-shell" in response.text
        assert f'data-lens-target="{lens}"' in response.text
        assert fragment in response.text
        assert "lensFromLocation" in response.text


def test_empty_dashboard_is_honest_and_useful(client) -> None:
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "오늘 상태를 준비하는 중입니다." in response.text
    assert "지금 결정할 제안이 없습니다." in response.text
    assert "이번 주 활성 목표가 없습니다." in response.text
    assert "아직 기록된 판단이 없습니다." in response.text
    assert "오늘 기록된 식사나 섭취가 없습니다." in response.text
    assert "임의의 행동이나 건강 원인을 만들어 표시하지 않습니다." in response.text


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
        locked = client.get(
            "/decisions/00000000-0000-0000-0000-000000000000?kind=alert"
        )
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
    proxied = settings.model_copy(
        update={"public_base_url": "https://example.test/healthmes"}
    )
    with TestClient(create_app(proxied)) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert 'href="/healthmes/dashboard#today"' in response.text
    assert 'href="/healthmes/dashboard/plan#plan"' in response.text
    assert 'href="/healthmes/dashboard/history#history"' in response.text
    assert 'href="/healthmes/connect"' in response.text
    assert 'href="/healthmes/sleep"' in response.text
    assert 'href="/healthmes/storage"' in response.text


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
    assert 'action="/healthmes/unlock"' in response.text


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

        response = client.get(
            "/dashboard", params={"token": viewer_token(TOKEN)}
        )

    assert response.status_code == 200
    expected = (
        f"http://healthmes.test:8100/decisions/{decision_id}"
        f"?token={viewer_token(TOKEN)}"
    )
    assert expected.replace("&", "&amp;") in response.text
    lens_link = f'/dashboard/plan?token={viewer_token(TOKEN)}#plan'
    assert f'href="{lens_link}"' in response.text
    assert TOKEN not in response.text


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
