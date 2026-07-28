from __future__ import annotations

import re

from fastapi.testclient import TestClient

from healthmes.api.sleep import SleepReviewRuntime
from healthmes.calendars.approval import ApprovalCalendar
from healthmes.calendars.sleep_proposal_state import redacted_digest
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
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
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
    from pydantic import SecretStr

    from healthmes.api.auth import viewer_token
    from healthmes.app import create_app

    token = "viewer-is-read-only"
    secured = settings.model_copy(update={"api_token": SecretStr(token)})
    with TestClient(create_app(secured), base_url="http://healthmes.test:8100") as client:
        response = client.post(
            f"/sleep/apply?token={viewer_token(token)}",
            data={},
            headers={"Origin": "http://healthmes.test:8100"},
        )
    assert response.status_code == 401
