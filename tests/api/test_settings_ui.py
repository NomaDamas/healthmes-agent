"""Unified settings and Decision Remote live-QA surface."""

import re
import stat

from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.app import create_app
from healthmes.store import ProposalStatus, ScheduleProposal

_CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def _local_client(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8100")


def test_settings_page_is_editable_only_from_direct_loopback(app) -> None:
    with _local_client(app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert 'action="/settings/save"' in response.text
    assert "테스트 결정 만들기" in response.text
    assert _CSRF.search(response.text)


def test_public_settings_is_viewer_only_and_never_renders_secret(settings) -> None:
    secret = "public-settings-full-api-token"
    tokened = settings.model_copy(update={"api_token": SecretStr(secret)})
    with TestClient(
        create_app(tokened),
        base_url="https://healthmes-agent.jinminseong.com",
    ) as client:
        response = client.get(
            "/settings",
            params={"token": viewer_token(secret)},
        )
        write = client.post(
            "/settings/save",
            params={"token": viewer_token(secret)},
            data={},
        )

    assert response.status_code == 200
    assert "Sake QA 링크" in response.text
    assert 'action="/settings/save"' not in response.text
    assert secret not in response.text
    assert write.status_code == 401


def test_settings_save_stages_restart_and_quotes_dotenv(app, tmp_path, monkeypatch) -> None:
    runtime_scheduler_enabled = app.state.settings.scheduler_enabled
    runtime_public_base_url = app.state.settings.public_base_url
    monkeypatch.chdir(tmp_path)
    with _local_client(app) as client:
        page = client.get("/settings")
        csrf = _CSRF.search(page.text)
        assert csrf is not None
        response = client.post(
            "/settings/save",
            headers={"Origin": "http://127.0.0.1:8100"},
            data={
                "csrf": csrf.group(1),
                "public_base_url": "https://healthmes-agent.jinminseong.com",
                "timezone": "Asia/Seoul",
                "scheduler_enabled": "true",
                "native_alert_delivery": "true",
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "07:30",
                "alert_daily_budget": "5",
                "alert_cooldown_minutes": "90",
                "google_calendar_id": "primary",
                "google_poll_minutes": "5",
                "caldav_url": "https://caldav.icloud.com",
                "caldav_calendar_name": "Health # Focus",
                "caldav_poll_minutes": "10",
                "ow_base_url": "http://localhost:8000",
                "ow_user_id": "",
                "ow_api_key": "",
                "backup_provider": "",
                "backup_passphrase": "",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=restart"
    assert app.state.settings.scheduler_enabled is runtime_scheduler_enabled
    assert app.state.settings.public_base_url == runtime_public_base_url
    assert app.state.pending_settings.scheduler_enabled is True
    assert app.state.pending_settings.public_base_url == ("https://healthmes-agent.jinminseong.com")
    dotenv = (tmp_path / ".env").read_text(encoding="utf-8")
    assert 'HEALTHMES_CALDAV_CALENDAR_NAME="Health # Focus"' in dotenv
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_settings_save_rejects_multiline_env_injection(app, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with _local_client(app) as client:
        page = client.get("/settings")
        csrf = _CSRF.search(page.text)
        assert csrf is not None
        response = client.post(
            "/settings/save",
            headers={"Origin": "http://127.0.0.1:8100"},
            data={
                "csrf": csrf.group(1),
                "public_base_url": ("https://healthmes.test\nHEALTHMES_API_TOKEN=attacker"),
                "timezone": "UTC",
                "quiet_hours_start": "22:30",
                "quiet_hours_end": "07:00",
                "alert_daily_budget": "8",
                "alert_cooldown_minutes": "60",
                "google_calendar_id": "primary",
                "google_poll_minutes": "5",
                "caldav_url": "https://caldav.test",
                "caldav_calendar_name": "",
                "caldav_poll_minutes": "10",
                "ow_base_url": "http://ow.test",
                "ow_user_id": "",
                "ow_api_key": "",
                "backup_provider": "",
                "backup_passphrase": "",
            },
        )

    assert response.status_code == 400
    assert not (tmp_path / ".env").exists()


def test_settings_save_rejects_invalid_timezone_and_zero_polling(
    app, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with _local_client(app) as client:
        page = client.get("/settings")
        csrf = _CSRF.search(page.text)
        assert csrf is not None
        common = {
            "csrf": csrf.group(1),
            "public_base_url": "https://healthmes-agent.jinminseong.com",
            "quiet_hours_start": "22:30",
            "quiet_hours_end": "07:00",
            "alert_daily_budget": "8",
            "alert_cooldown_minutes": "60",
            "google_calendar_id": "primary",
            "google_poll_minutes": "5",
            "caldav_url": "https://caldav.icloud.com",
            "caldav_calendar_name": "",
            "caldav_poll_minutes": "10",
            "ow_base_url": "http://localhost:8000",
            "ow_user_id": "",
            "ow_api_key": "",
            "backup_provider": "",
            "backup_passphrase": "",
        }
        invalid_timezone = client.post(
            "/settings/save",
            headers={"Origin": "http://127.0.0.1:8100"},
            data={**common, "timezone": "Mars/Olympus_Mons"},
        )
        invalid_polling = client.post(
            "/settings/save",
            headers={"Origin": "http://127.0.0.1:8100"},
            data={**common, "timezone": "Asia/Seoul", "google_poll_minutes": "0"},
        )
        invalid_provider = client.post(
            "/settings/save",
            headers={"Origin": "http://127.0.0.1:8100"},
            data={
                **common,
                "timezone": "Asia/Seoul",
                "backup_provider": "shell-command",
            },
        )

    assert invalid_timezone.status_code == 400
    assert invalid_polling.status_code == 400
    assert invalid_provider.status_code == 400
    assert not (tmp_path / ".env").exists()


def test_live_qa_creates_actionable_schedule_proposal(app, session) -> None:
    with _local_client(app) as client:
        page = client.get("/settings")
        csrf = _CSRF.search(page.text)
        assert csrf is not None
        response = client.post(
            "/settings/qa/decision",
            headers={"Origin": "http://127.0.0.1:8100"},
            data={"csrf": csrf.group(1)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?qa=created"
    proposal = session.query(ScheduleProposal).one()
    assert proposal.status is ProposalStatus.PROPOSED
    assert proposal.reply_handle_digest
    assert proposal.decision_record_id is not None
