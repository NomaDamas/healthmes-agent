"""Calendar-connection status page: ``GET /connect`` (status + instructions only).

Read-only by design. The page performs NO writes, triggers no OAuth flow and
renders NO secret — not the API token, not OAuth tokens, not app passwords,
and not even the connected account's username. Connection state is derived
from file presence / env flags via :mod:`healthmes.calendars.creds` (offline,
no network). The actual connect/disconnect actions live in the CLI
(``healthmes connect ...``), which runs on the machine that owns the data dir
— a hosted web-OAuth button would need a registered redirect URI and secret
handling inside the service and is deliberately out of scope (noted as future
work in docs/DEVELOPMENT.md 캘린더 연결).

Gating matches the other human-facing viewer pages (``/decisions``,
``/reports``): the shared bearer middleware applies, and as a GET page under a
``VIEWER_PATH_PREFIXES`` entry it additionally accepts the derived read-only
``?token=`` viewer credential (healthmes/api/auth.py) so a phone browser can
open it from a link.
"""

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from healthmes.api.decision_html import shell_context, template_environment
from healthmes.calendars import creds
from healthmes.calendars.google import google_client_secret_path, google_token_path
from healthmes.config import Settings

__all__ = ["router", "build_connection_cards", "ConnectionCard"]

router = APIRouter(tags=["connect"])

CONNECT_PATH = "/connect"

# Exact commands the page shows for not-connected calendars (docs/DEVELOPMENT.md
# uses `uv run` as the canonical invocation; `healthmes` is the console script).
GOOGLE_CONNECT_COMMAND = "uv run healthmes connect google"
ICLOUD_CONNECT_COMMAND = "uv run healthmes connect icloud --username you@icloud.com"


@dataclass(frozen=True)
class ConnectionCard:
    """Everything the template needs for one calendar — no secret material.

    Every string here is built server-side from static text and data-dir
    paths; nothing user- or credential-derived ever lands in a field.
    """

    key: str
    label: str
    connected: bool
    detail: str
    """Short status line: credential *source* and path — never values."""
    command: str = ""
    """Exact CLI command to run when not connected."""
    steps: tuple[str, ...] = ()
    """One-time prerequisite steps (Google OAuth-client registration)."""
    link_label: str = ""
    link_url: str = ""
    notes: tuple[str, ...] = ()


def _google_card(settings: Settings) -> ConnectionCard:
    state = creds.google_connection_state(settings.data_dir)
    token_path = google_token_path(settings.data_dir)
    client_secret = google_client_secret_path(settings.data_dir)
    if state == "connected":
        return ConnectionCard(
            key="google",
            label="Google Calendar",
            connected=True,
            detail=f"OAuth 토큰 저장됨 · {token_path}",
        )
    notes: list[str] = []
    if state == "invalid":
        notes.append(
            "저장된 토큰 파일이 손상되었습니다 — `uv run healthmes connect "
            "disconnect google` 후 다시 연결하세요."
        )
    if settings.google_calendar_enabled:
        notes.append(
            "HEALTHMES_GOOGLE_CALENDAR_ENABLED=true 로 폴링은 켜져 있지만, "
            "토큰이 없어 매 주기 실패합니다."
        )
    steps: tuple[str, ...] = ()
    has_client_secret = client_secret.exists() or (
        settings.google_client_secret_file is not None
        and settings.google_client_secret_file.exists()
    )
    if not has_client_secret:
        steps = (
            "console.cloud.google.com 에서 프로젝트 생성 (또는 선택)",
            "“APIs & Services → Library”에서 Google Calendar API 활성화",
            "“OAuth consent screen” 구성 후 본인 계정을 테스트 사용자로 추가",
            "“Credentials → Create credentials → OAuth client ID”에서 "
            "유형 “Desktop app”으로 생성",
            f"내려받은 JSON을 {client_secret} 에 저장",
        )
    return ConnectionCard(
        key="google",
        label="Google Calendar",
        connected=False,
        detail="미연결 — 한 번의 브라우저 로그인으로 연결됩니다.",
        command=GOOGLE_CONNECT_COMMAND,
        steps=steps,
        notes=tuple(notes),
    )


def _icloud_card(settings: Settings) -> ConnectionCard:
    resolved = creds.resolve_caldav_credentials(settings)
    if resolved is not None:
        if resolved.source == "env":
            detail = "환경변수(.env)의 HEALTHMES_CALDAV_* 자격증명 사용 중"
        else:
            detail = (
                "CLI로 저장된 자격증명 사용 중 · "
                f"{creds.caldav_credentials_path(settings.data_dir)}"
            )
        return ConnectionCard(
            key="icloud",
            label="iCloud 캘린더 (CalDAV)",
            connected=True,
            detail=detail,
        )
    return ConnectionCard(
        key="icloud",
        label="iCloud 캘린더 (CalDAV)",
        connected=False,
        detail="미연결 — 앱 암호 한 번 입력으로 연결됩니다 (숨김 프롬프트).",
        command=ICLOUD_CONNECT_COMMAND,
        link_label="앱 암호 만들기 (appleid.apple.com)",
        link_url="https://appleid.apple.com",
    )


def build_connection_cards(settings: Settings) -> list[ConnectionCard]:
    """Connection status of every supported calendar (pure, offline, no secrets)."""
    return [_google_card(settings), _icloud_card(settings)]


@router.get(CONNECT_PATH, response_class=HTMLResponse)
def connect_status_page(request: Request) -> HTMLResponse:
    """Human-facing calendar-connection status page (read-only)."""
    settings: Settings = request.app.state.settings
    template = template_environment().get_template("ui/connect.html.j2")
    html = template.render(
        cards=build_connection_cards(settings),
        scheduler_enabled=settings.scheduler_enabled,
        **shell_context(settings),
    )
    return HTMLResponse(html)
