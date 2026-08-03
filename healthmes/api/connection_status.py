from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from healthmes.calendars import creds
from healthmes.calendars.google_client import resolve_google_client_secret
from healthmes.config import Settings, resolve_timezone
from healthmes.mcp_server.ow_client import (
    OWAuthError,
    OWClient,
    OWClientError,
    OWConfigurationError,
    resolve_single_user_id,
)

OURA_FRESHNESS_LIMIT = timedelta(hours=48)
GOOGLE_CONNECT_COMMAND = "uv run healthmes connect google"
ICLOUD_CONNECT_COMMAND = "uv run healthmes connect icloud --username you@icloud.com"


@dataclass(frozen=True, slots=True)
class ConnectionCard:
    key: str
    label: str
    connected: bool
    detail: str
    command: str = ""
    steps: tuple[str, ...] = ()
    link_label: str = ""
    link_url: str = ""
    notes: tuple[str, ...] = ()
    badge_label: str = ""


class OpenWearablesStatusReader(Protocol):
    async def list_users(
        self, *, search: str | None = None, limit: int = 100
    ) -> Mapping[str, Any]: ...
    async def get_connections(self, user_id: str) -> object: ...
    async def collect_sleep_summaries(
        self, user_id: str, start_date: str, end_date: str
    ) -> object: ...


class ProviderPayloadError(ValueError):
    """A successful provider response did not match its documented shape."""


async def build_oura_card(
    settings: Settings,
    *,
    client: OpenWearablesStatusReader | None = None,
    now: datetime | None = None,
) -> ConnectionCard:
    if not settings.ow_api_key.get_secret_value():
        return ConnectionCard(
            "oura",
            "Oura · Open Wearables",
            False,
            "Open Wearables API key 미설정",
            badge_label="설정 필요",
            notes=("HEALTHMES_OW_API_KEY를 설정하면 연결·freshness를 확인합니다.",),
        )
    reader = client or OWClient.from_settings(settings)
    try:
        user_id = await resolve_single_user_id(reader, settings)
        connections = _mapping_rows(await reader.get_connections(user_id))
    except OWConfigurationError:
        return _oura_error("Open Wearables API key 미설정")
    except OWAuthError:
        return _oura_error("Open Wearables API key 인증 실패")
    except LookupError:
        return _oura_error("Open Wearables 사용자 선택 필요")
    except OWClientError:
        return _oura_error(
            "Open Wearables API 연결 또는 응답 오류",
            notes=("Open Wearables 서비스 상태와 API 버전을 확인하세요.",),
        )
    except (AttributeError, TypeError, ValueError):
        return _oura_payload_error("연결")
    connection = next(
        (
            row
            for row in connections
            if isinstance(row, Mapping)
            and str(row.get("provider", "")).strip().lower() == "oura"
        ),
        None,
    )
    if connection is None:
        return ConnectionCard("oura", "Oura · Open Wearables", False, "Oura 미연결")
    if str(connection.get("status", "")).strip().lower() != "active":
        return ConnectionCard(
            "oura",
            "Oura · Open Wearables",
            False,
            "Oura 연결 비활성",
            badge_label="확인 필요",
        )
    current = (now or datetime.now(UTC)).astimezone(resolve_timezone(settings))
    try:
        rows = _mapping_rows(
            await reader.collect_sleep_summaries(
                user_id,
                (current.date() - timedelta(days=7)).isoformat(),
                (current.date() + timedelta(days=1)).isoformat(),
            )
        )
    except OWClientError:
        return ConnectionCard(
            "oura",
            "Oura · Open Wearables",
            False,
            "Open Wearables 수면 데이터 확인 오류",
            badge_label="오류",
            notes=("Open Wearables 서비스 상태와 API 버전을 확인하세요.",),
        )
    except (AttributeError, TypeError, ValueError):
        return _oura_payload_error("수면 데이터")
    freshness, fresh = _sleep_freshness(rows, current.date()) or _sync_freshness(
        _parse_datetime(connection.get("last_synced_at")),
        current.astimezone(UTC),
    )
    return ConnectionCard(
        "oura",
        "Oura · Open Wearables",
        True,
        f"Oura 연결됨 · {freshness}",
        notes=() if fresh else ("Oura 동기화 데이터가 오래되었습니다.",),
    )


def build_connection_cards(settings: Settings) -> list[ConnectionCard]:
    return [_google_card(settings), _icloud_card(settings)]


def _google_card(settings: Settings) -> ConnectionCard:
    state = creds.google_connection_state(settings.data_dir)
    if state == "connected":
        return ConnectionCard(
            "google", "Google Calendar", True, "Google OAuth credential 확인됨"
        )
    notes: list[str] = []
    if state == "invalid":
        notes.append("저장된 토큰 파일이 손상되었습니다. 다시 연결하세요.")
    if settings.google_calendar_enabled:
        notes.append("캘린더 폴링은 켜져 있지만 토큰이 없어 실패합니다.")
    steps = ()
    if resolve_google_client_secret(settings) is None:
        steps = (
            "console.cloud.google.com 에서 프로젝트를 생성하거나 선택",
            "Google Calendar API 활성화",
            "OAuth consent screen 구성 후 본인 계정을 테스트 사용자로 추가",
            "Desktop app 유형 OAuth client ID 생성",
            "내려받은 JSON을 HealthMes data directory에 저장",
        )
    return ConnectionCard(
        "google",
        "Google Calendar",
        False,
        "미연결 — 이 Mac의 UI에서 Google 로그인으로 연결합니다.",
        command=GOOGLE_CONNECT_COMMAND,
        steps=steps,
        notes=tuple(notes),
    )


def _icloud_card(settings: Settings) -> ConnectionCard:
    resolved = creds.resolve_caldav_credentials(settings)
    if resolved is not None:
        detail = (
            "환경변수(.env)의 HEALTHMES_CALDAV_* 자격증명 사용 중"
            if resolved.source == "env"
            else "CLI로 저장된 CalDAV credential 사용 중"
        )
        return ConnectionCard("icloud", "iCloud 캘린더 (CalDAV)", True, detail)
    return ConnectionCard(
        "icloud",
        "iCloud 캘린더 (CalDAV)",
        False,
        "미연결 — 앱 암호 한 번 입력으로 연결됩니다 (숨김 프롬프트).",
        command=ICLOUD_CONNECT_COMMAND,
        link_label="앱 암호 만들기 (appleid.apple.com)",
        link_url="https://appleid.apple.com",
    )


def _oura_error(
    detail: str,
    *,
    notes: tuple[str, ...] = (),
) -> ConnectionCard:
    return ConnectionCard(
        "oura",
        "Oura · Open Wearables",
        False,
        detail,
        badge_label="오류",
        notes=notes,
    )


def _oura_payload_error(stage: str) -> ConnectionCard:
    return _oura_error(
        f"Open Wearables {stage} 응답 형식 오류",
        notes=("Open Wearables를 업데이트하거나 서비스 로그를 확인하세요.",),
    )


def _mapping_rows(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise ProviderPayloadError("provider response must be a row sequence")
    rows = tuple(row for row in value if isinstance(row, Mapping))
    if value and not rows:
        raise ProviderPayloadError("provider response contains no valid rows")
    return rows


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)


def _sync_freshness(value: datetime | None, now: datetime) -> tuple[str, bool]:
    if value is None:
        return "마지막 sync 기록 없음", False
    age = max(now - value, timedelta())
    if age < timedelta(hours=1):
        label = "마지막 sync 1시간 이내"
    elif age < timedelta(days=1):
        label = f"마지막 sync {int(age.total_seconds() // 3600)}시간 전"
    else:
        label = f"마지막 sync {int(age.total_seconds() // 86400)}일 전"
    return label, age <= OURA_FRESHNESS_LIMIT


def _sleep_freshness(
    rows: Sequence[object],
    today: date,
) -> tuple[str, bool] | None:
    dates: list[date] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get("date") or row.get("local_date") or row.get("calendar_date")
        if not isinstance(value, str):
            continue
        try:
            dates.append(date.fromisoformat(value[:10]))
        except ValueError:
            continue
    if not dates:
        return None
    days = max((today - max(dates)).days, 0)
    label = "최신 수면 오늘" if days == 0 else f"최신 수면 {days}일 전"
    return label, days <= 2
