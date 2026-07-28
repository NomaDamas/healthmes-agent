"""Runtime-connection status page: ``GET /connect`` (status + instructions only).

Read-only by design. The page performs NO writes, triggers no OAuth flow and
renders NO secret — not the API token, not OAuth tokens, not app passwords,
provider identifiers, or connected usernames. Calendar state is derived from
file presence / env flags via :mod:`healthmes.calendars.creds` (offline); the
Oura card uses read-only Open Wearables endpoints to report connection and
freshness. The actual connect/disconnect actions live in the CLI
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
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from healthmes.api.decision_html import shell_context, template_environment
from healthmes.calendars import creds
from healthmes.calendars.google import google_client_secret_path
from healthmes.config import Settings, resolve_timezone
from healthmes.mcp_server.ow_client import (
    OWAuthError,
    OWClient,
    OWClientError,
    OWConfigurationError,
    resolve_single_user_id,
)

__all__ = ["router", "build_connection_cards", "build_oura_card", "ConnectionCard"]

router = APIRouter(tags=["connect"])

CONNECT_PATH = "/connect"
OURA_FRESHNESS_LIMIT = timedelta(hours=48)

# Exact commands the page shows for not-connected calendars (docs/DEVELOPMENT.md
# uses `uv run` as the canonical invocation; `healthmes` is the console script).
GOOGLE_CONNECT_COMMAND = "uv run healthmes connect google"
ICLOUD_CONNECT_COMMAND = "uv run healthmes connect icloud --username you@icloud.com"


@dataclass(frozen=True)
class ConnectionCard:
    """Everything the template needs for one connection — no secret material.

    Every string here is built server-side from static text; nothing user- or
    credential-derived ever lands in a field.
    """

    key: str
    label: str
    connected: bool
    detail: str
    """Short status line — never credential values, paths, or user identifiers."""
    command: str = ""
    """Exact CLI command to run when not connected."""
    steps: tuple[str, ...] = ()
    """One-time prerequisite steps (Google OAuth-client registration)."""
    link_label: str = ""
    link_url: str = ""
    notes: tuple[str, ...] = ()
    badge_label: str = ""


class OpenWearablesStatusReader(Protocol):
    async def list_users(self, *, search: str | None = None, limit: int = 100) -> dict: ...

    async def get_connections(self, user_id: str) -> list[dict]: ...

    async def collect_sleep_summaries(
        self, user_id: str, start_date: str, end_date: str
    ) -> list[dict]: ...


def _parse_remote_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _freshness_detail(last_synced_at: datetime | None, now: datetime) -> tuple[str, bool]:
    if last_synced_at is None:
        return "마지막 sync 기록 없음", False
    age = max(now - last_synced_at, timedelta())
    if age < timedelta(hours=1):
        label = "마지막 sync 1시간 이내"
    elif age < timedelta(days=1):
        label = f"마지막 sync {int(age.total_seconds() // 3600)}시간 전"
    else:
        label = f"마지막 sync {int(age.total_seconds() // 86400)}일 전"
    return label, age <= OURA_FRESHNESS_LIMIT


def _sleep_freshness_detail(rows: list[dict], today: date) -> tuple[str, bool] | None:
    dates: list[date] = []
    for row in rows:
        value = row.get("date") or row.get("local_date") or row.get("calendar_date")
        if not isinstance(value, str):
            continue
        try:
            dates.append(date.fromisoformat(value[:10]))
        except ValueError:
            continue
    if not dates:
        return None
    age_days = max((today - max(dates)).days, 0)
    if age_days == 0:
        label = "최신 수면 오늘"
    elif age_days == 1:
        label = "최신 수면 1일 전"
    else:
        label = f"최신 수면 {age_days}일 전"
    return label, age_days <= 2


async def build_oura_card(
    settings: Settings,
    *,
    client: OpenWearablesStatusReader | None = None,
    now: datetime | None = None,
) -> ConnectionCard:
    if not settings.ow_api_key.get_secret_value():
        return ConnectionCard(
            key="oura",
            label="Oura · Open Wearables",
            connected=False,
            badge_label="설정 필요",
            detail="Open Wearables API key 미설정",
            notes=("HEALTHMES_OW_API_KEY를 설정하면 연결·freshness를 확인합니다.",),
        )

    reader = client or OWClient.from_settings(settings)
    try:
        user_id = await resolve_single_user_id(reader, settings)
        connections = await reader.get_connections(user_id)
    except OWConfigurationError:
        detail = "Open Wearables API key 미설정"
    except OWAuthError:
        detail = "Open Wearables API key 인증 실패"
    except LookupError:
        detail = "Open Wearables 사용자 선택 필요"
    except OWClientError:
        detail = "Open Wearables API 연결 오류"
    else:
        connection = next(
            (
                row
                for row in connections
                if str(row.get("provider", "")).strip().lower() == "oura"
            ),
            None,
        )
        if connection is None:
            return ConnectionCard(
                key="oura",
                label="Oura · Open Wearables",
                connected=False,
                detail="Oura 미연결",
            )
        if str(connection.get("status", "")).strip().lower() != "active":
            return ConnectionCard(
                key="oura",
                label="Oura · Open Wearables",
                connected=False,
                badge_label="확인 필요",
                detail="Oura 연결 비활성",
            )
        current = (now or datetime.now(UTC)).astimezone(resolve_timezone(settings))
        try:
            sleep_rows = await reader.collect_sleep_summaries(
                user_id,
                (current.date() - timedelta(days=7)).isoformat(),
                (current.date() + timedelta(days=1)).isoformat(),
            )
        except OWClientError:
            return ConnectionCard(
                key="oura",
                label="Oura · Open Wearables",
                connected=False,
                badge_label="오류",
                detail="Open Wearables 수면 데이터 확인 오류",
            )
        sleep_freshness = _sleep_freshness_detail(sleep_rows, current.date())
        freshness, fresh = sleep_freshness or _freshness_detail(
            _parse_remote_datetime(connection.get("last_synced_at")),
            current.astimezone(UTC),
        )
        notes = () if fresh else ("Oura 동기화 데이터가 오래되었습니다.",)
        return ConnectionCard(
            key="oura",
            label="Oura · Open Wearables",
            connected=True,
            detail=f"Oura 연결됨 · {freshness}",
            notes=notes,
        )

    return ConnectionCard(
        key="oura",
        label="Oura · Open Wearables",
        connected=False,
        badge_label="오류",
        detail=detail,
    )


def _google_card(settings: Settings) -> ConnectionCard:
    state = creds.google_connection_state(settings.data_dir)
    client_secret = google_client_secret_path(settings.data_dir)
    if state == "connected":
        return ConnectionCard(
            key="google",
            label="Google Calendar",
            connected=True,
            detail="Google OAuth credential 확인됨",
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
            "내려받은 JSON을 HealthMes data directory의 "
            "google/calendar_client_secret.json 에 저장",
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
            detail = "CLI로 저장된 CalDAV credential 사용 중"
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
async def connect_status_page(request: Request) -> HTMLResponse:
    """Human-facing runtime-connection status page (read-only)."""
    settings: Settings = request.app.state.settings
    template = template_environment().get_template("ui/connect.html.j2")
    html = template.render(
        cards=[await build_oura_card(settings), *build_connection_cards(settings)],
        scheduler_enabled=settings.scheduler_enabled,
        **shell_context(settings),
    )
    return HTMLResponse(html)
