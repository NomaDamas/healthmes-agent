from __future__ import annotations

import re
import uuid

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from healthmes.api.auth import viewer_token
from healthmes.api.sleep import SleepReviewRuntime
from healthmes.app import create_app
from healthmes.calendars.approval import ApprovalCalendar
from healthmes.calendars.sleep_proposal_state import redacted_digest
from healthmes.store import SleepReconciliationProposal
from healthmes.store.session import get_session
from tests.calendars.conftest import FakeCalendarBackend


class SleepReader:
    async def collect_sleep_summaries(self, user_id, start_date, end_date):
        assert user_id == "redacted-user"
        return [
            {
                "date": "2026-07-26",
                "source": {"provider": "oura"},
                "start_time": "2026-07-25T23:00:00+09:00",
                "end_time": "2026-07-26T07:00:00+09:00",
                "duration_minutes": 420,
                "time_in_bed_minutes": 450,
            }
        ]


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_local_sleep_review_applies_exact_preview_and_shows_read_back(
    app,
) -> None:
    fake_backend = FakeCalendarBackend()
    app.state.settings = app.state.settings.model_copy(
        update={"timezone": "Asia/Seoul"}
    )
    app.state.sleep_review_runtime = SleepReviewRuntime(
        SleepReader(),
        "redacted-user",
        ApprovalCalendar(fake_backend, fake_backend.approval_target),
    )
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        preview = client.get("/sleep?date=2026-07-26")

        assert preview.status_code == 200
        assert "would_create" in preview.text
        assert "2026-07-25 23:00:00 KST" in preview.text
        assert "2026-07-26 07:00:00 KST" in preview.text
        assert "실제 수면 420분 · 침대 구간 450분 · 구간 내 비수면 30분" in preview.text
        assert "Oura 수면 세션" in preview.text
        assert redacted_digest(fake_backend.approval_target) in preview.text
        assert "이 preview를 Calendar에 반영" in preview.text
        cookie = preview.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        form = {
            "proposal_id": _hidden(preview.text, "proposal_id"),
            "csrf": _hidden(preview.text, "csrf"),
            "approval": _hidden(preview.text, "approval"),
        }

        rejected = client.post("/sleep/apply", data=form)
        assert rejected.status_code == 403
        assert fake_backend.created_drafts == []

        applied = client.post(
            "/sleep/apply",
            data=form,
            headers={"Origin": "http://127.0.0.1:8100"},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        assert len(fake_backend.created_drafts) == 1

        receipt = client.get(applied.headers["location"])
        assert "Fresh provider read-back" in receipt.text
        assert "검증 완료" in receipt.text
        assert "fake-refresh-token" not in receipt.text


def test_viewer_token_cannot_authorize_sleep_post(settings) -> None:
    token = "viewer-is-read-only"
    secured = settings.model_copy(update={"api_token": SecretStr(token)})
    with TestClient(create_app(secured), base_url="http://healthmes.test:8100") as client:
        response = client.post(
            f"/sleep/apply?token={viewer_token(token)}",
            data={},
            headers={"Origin": "http://healthmes.test:8100"},
        )
    assert response.status_code == 401


def test_viewer_token_get_does_not_persist_sleep_proposal(
    settings,
    session_factory,
    session,
) -> None:
    token = "viewer-must-stay-read-only"
    secured = settings.model_copy(update={"api_token": SecretStr(token)})
    app = create_app(secured)

    def _override_get_session():
        database_session = session_factory()
        try:
            yield database_session
        finally:
            database_session.close()

    app.dependency_overrides[get_session] = _override_get_session
    fake_backend = FakeCalendarBackend()
    app.state.sleep_review_runtime = SleepReviewRuntime(
        SleepReader(),
        "redacted-user",
        ApprovalCalendar(fake_backend, fake_backend.approval_target),
    )
    with TestClient(app, base_url="http://healthmes.test:8100") as client:
        response = client.get("/sleep", params={"token": viewer_token(token)})
        assert response.status_code == 200
        assert 'name="api_token"' not in response.text
        count = session.scalar(
            select(func.count()).select_from(SleepReconciliationProposal)
        )
        assert count == 0


def test_loopback_viewer_unlocks_sleep_without_token_in_redirect(settings) -> None:
    token = "sleep-local-api-token"
    secured = settings.model_copy(update={"api_token": SecretStr(token)})
    with TestClient(
        create_app(secured),
        base_url="http://127.0.0.1:8100",
    ) as client:
        viewer = client.get("/sleep", params={"token": viewer_token(token)})
        assert viewer.status_code == 200
        assert 'href="http://127.0.0.1:8100/sleep/unlock"' in viewer.text
        assert 'name="api_token"' not in viewer.text
        assert "healthmes_local_session" not in viewer.headers.get("set-cookie", "")

        unlock_page = client.get("/sleep/unlock")
        assert unlock_page.status_code == 200
        assert 'action="http://127.0.0.1:8100/sleep/unlock"' in unlock_page.text
        assert 'name="api_token"' in unlock_page.text

        unlocked = client.post(
            "/sleep/unlock",
            data={"api_token": token},
            headers={"Origin": "http://127.0.0.1:8100"},
            follow_redirects=False,
        )
        assert unlocked.status_code == 303
        assert unlocked.headers["location"] == "/sleep"
        assert token not in unlocked.headers["location"]
        assert token not in unlocked.text
        assert "healthmes_local_session" in unlocked.headers["set-cookie"]


def test_malformed_persisted_sleep_segment_renders_safe_error(app, session) -> None:
    fake_backend = FakeCalendarBackend()
    app.state.sleep_review_runtime = SleepReviewRuntime(
        SleepReader(),
        "redacted-user",
        ApprovalCalendar(fake_backend, fake_backend.approval_target),
    )
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        preview = client.get("/sleep?date=2026-07-26")
        proposal_id = uuid.UUID(_hidden(preview.text, "proposal_id"))
        proposal = session.get(SleepReconciliationProposal, proposal_id)
        assert proposal is not None
        proposal.snapshot = {
            **proposal.snapshot,
            "segments": [
                {
                    "wake_time": "2026-07-26T07:00:00+09:00",
                    "duration_minutes": 30,
                }
            ],
        }
        session.commit()

        rendered = client.get(f"/sleep?proposal={proposal_id}")
        assert rendered.status_code == 200
        assert "저장된 수면 preview를 표시할 수 없습니다." in rendered.text
        assert "이 preview를 Calendar에 반영" not in rendered.text
