"""Owner-only web setup and Decision Remote live-QA surface."""

from __future__ import annotations

import datetime as dt
import os
import stat
import tempfile
import zoneinfo
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import SecretStr, ValidationError

from healthmes.api.auth import viewer_token, viewer_url
from healthmes.api.connection_status import build_connection_cards, build_oura_card
from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.local_session import (
    bootstrap_local_session,
    is_loopback_scope,
    issue_local_session,
    local_browser_url,
    require_local_session,
)
from healthmes.api.sleep import _form
from healthmes.calendars.adjustments import issue_reply_handle
from healthmes.config import Settings, resolve_timezone
from healthmes.store import (
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TaskSource,
    TriggerEvent,
)
from healthmes.store.session import SessionDep

router = APIRouter(tags=["settings"])

EDITABLE_FIELDS: dict[str, tuple[str, bool]] = {
    "public_base_url": ("HEALTHMES_PUBLIC_BASE_URL", False),
    "timezone": ("HEALTHMES_TIMEZONE", False),
    "scheduler_enabled": ("HEALTHMES_SCHEDULER_ENABLED", False),
    "native_alert_delivery": ("HEALTHMES_NATIVE_ALERT_DELIVERY", False),
    "quiet_hours_start": ("HEALTHMES_QUIET_HOURS_START", False),
    "quiet_hours_end": ("HEALTHMES_QUIET_HOURS_END", False),
    "alert_daily_budget": ("HEALTHMES_ALERT_DAILY_BUDGET", False),
    "alert_cooldown_minutes": ("HEALTHMES_ALERT_COOLDOWN_MINUTES", False),
    "google_calendar_id": ("HEALTHMES_GOOGLE_CALENDAR_ID", False),
    "google_poll_minutes": ("HEALTHMES_GOOGLE_POLL_MINUTES", False),
    "caldav_url": ("HEALTHMES_CALDAV_URL", False),
    "caldav_calendar_name": ("HEALTHMES_CALDAV_CALENDAR_NAME", False),
    "caldav_poll_minutes": ("HEALTHMES_CALDAV_POLL_MINUTES", False),
    "ow_base_url": ("HEALTHMES_OW_BASE_URL", False),
    "ow_user_id": ("HEALTHMES_OW_USER_ID", False),
    "ow_api_key": ("HEALTHMES_OW_API_KEY", True),
    "backup_provider": ("HEALTHMES_BACKUP_PROVIDER", False),
    "backup_passphrase": ("HEALTHMES_BACKUP_PASSPHRASE", True),
}


@router.get("/settings/unlock", response_class=HTMLResponse)
async def settings_unlock_page(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    if not is_loopback_scope(request.scope) or not settings.api_token.get_secret_value().strip():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "local browser required")
    template = template_environment().get_template("ui/local_unlock.html.j2")
    html = template.render(
        heading="HealthMes 설정 잠금 해제",
        description="전체 API 토큰은 이 Mac의 HealthMes에만 전송됩니다.",
        post_url=local_browser_url(settings.port, "/settings/unlock"),
        active_nav="settings",
        **shell_context(settings),
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"},
    )


@router.post("/settings/unlock")
async def unlock_settings_page(request: Request) -> RedirectResponse:
    response = RedirectResponse("/settings", status_code=303)
    await bootstrap_local_session(request, response)
    return response


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    response: Response,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    configured_settings: Settings = getattr(
        request.app.state,
        "pending_settings",
        settings,
    )
    local = issue_local_session(request, response)
    api_token = settings.api_token.get_secret_value().strip()
    template = template_environment().get_template("ui/settings.html.j2")
    html = template.render(
        settings=configured_settings,
        runtime_settings=settings,
        cards=[await build_oura_card(settings), *build_connection_cards(settings)],
        local_session=local,
        local_unlock_url=(
            local_browser_url(
                settings.port,
                f"/settings/unlock?token={viewer_token(api_token)}",
            )
            if local is None and api_token
            else ""
        ),
        decision_qa_url=viewer_url(settings, "/decisions"),
        weekly_qa_url=viewer_url(settings, "/reports/weekly"),
        restart_required=request.query_params.get("saved") == "restart",
        qa=request.query_params.get("qa", ""),
        active_nav="settings",
        **shell_context(settings),
    )
    return HTMLResponse(
        html,
        headers={
            **response.headers,
            "Cache-Control": "no-store",
            "Referrer-Policy": "same-origin",
        },
    )


@router.post("/settings/save")
async def save_settings(request: Request) -> RedirectResponse:
    form = await _form(request)
    require_local_session(request, csrf_token=form.get("csrf", ""))
    current: Settings = request.app.state.settings
    values = _validated_values(form, current)
    pending = _candidate_settings(values, current)
    _update_dotenv(
        Path(".env"),
        {
            EDITABLE_FIELDS[name][0]: value
            for name, value in values.items()
            if not EDITABLE_FIELDS[name][1] or value
        },
    )
    request.app.state.pending_settings = pending
    return RedirectResponse("/settings?saved=restart", status_code=303)


@router.post("/settings/qa/decision")
async def create_qa_decision(
    request: Request,
    session: SessionDep,
) -> RedirectResponse:
    form = await _form(request)
    require_local_session(request, csrf_token=form.get("csrf", ""))
    settings: Settings = request.app.state.settings
    secret = settings.calendar_adjustment_secret.get_secret_value().strip()
    if len(secret) < 32:
        return RedirectResponse(
            "/settings?" + urlencode({"qa": "approval-secret-missing"}),
            status_code=303,
        )

    now = dt.datetime.now(dt.UTC)
    local_now = now.astimezone(resolve_timezone(settings))
    next_day = local_now.date() + dt.timedelta(days=1)
    proposed_start = dt.datetime.combine(
        next_day,
        dt.time(9, 30),
        tzinfo=resolve_timezone(settings),
    ).astimezone(dt.UTC)
    proposed_end = proposed_start + dt.timedelta(minutes=90)
    handle = issue_reply_handle(secret)

    task = Task(
        title="Decision Remote Live QA 집중 업무",
        est_minutes=90,
        deadline=proposed_end + dt.timedelta(hours=6),
        energy_demand=EnergyDemand.HIGH,
        status="scheduled",
        source=TaskSource.AGENT,
    )
    event = TriggerEvent(
        fired_at=now,
        rule_id="decision_remote_live_qa",
        payload={
            "summary": "회복이 낮아 오늘 고강도 업무를 미루는 편이 안전합니다.",
            "proposal": "집중 업무를 내일 09:30으로 옮길까요?",
            "evidence": {"recovery": "low", "qa": True},
            "severity": "normal",
        },
        alert_sent=True,
        dedup_key=f"decision-remote-live-qa:{now.isoformat()}",
    )
    session.add_all([task, event])
    session.flush()
    decision = DecisionRecord(
        kind=DecisionKind.ALERT,
        summary="Live QA: 집중 업무를 내일 오전으로 이동",
        trigger_event_id=event.id,
        tree={
            "id": "qa-root",
            "type": "rule",
            "label": "회복 저하 + 고강도 일정",
            "detail": "Decision Remote 실기기 QA를 위한 제안",
            "children": [
                {
                    "id": "qa-action",
                    "type": "action",
                    "label": "내일 09:30-11:00 집중 업무 배치",
                    "children": [],
                }
            ],
        },
    )
    session.add(decision)
    session.flush()
    session.add(
        ScheduleProposal(
            task_id=task.id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.PROPOSED,
            decision_record_id=decision.id,
            reply_handle_digest=handle.digest,
            expires_at=now + dt.timedelta(hours=2),
            healthmes_kind="schedule_change",
        )
    )
    session.commit()
    return RedirectResponse("/settings?qa=created", status_code=303)


def _validated_values(form: dict[str, str], current: Settings) -> dict[str, str]:
    values = {name: form.get(name, "").strip() for name in EDITABLE_FIELDS}
    for name in ("scheduler_enabled", "native_alert_delivery"):
        values[name] = "true" if form.get(name) == "true" else "false"
    public_url = urlsplit(values["public_base_url"])
    if public_url.scheme not in {"http", "https"} or not public_url.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid public base URL")
    for name in (
        "alert_daily_budget",
        "alert_cooldown_minutes",
    ):
        try:
            if int(values[name]) < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid {name}") from exc
    for name in ("google_poll_minutes", "caldav_poll_minutes"):
        try:
            if int(values[name]) < 1:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid {name}") from exc
    for name in ("quiet_hours_start", "quiet_hours_end"):
        try:
            dt.time.fromisoformat(values[name])
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid {name}") from exc
    if not values["timezone"]:
        values["timezone"] = current.timezone or ""
    if values["timezone"]:
        try:
            zoneinfo.ZoneInfo(values["timezone"])
        except zoneinfo.ZoneInfoNotFoundError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "invalid timezone",
            ) from exc
    if values["backup_provider"] not in {"", "local", "remote_vault", "remote"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "invalid backup_provider",
        )
    return values


def _settings_updates(values: dict[str, str], current: Settings) -> dict[str, object]:
    updates: dict[str, object] = {
        "public_base_url": values["public_base_url"],
        "timezone": values["timezone"] or None,
        "scheduler_enabled": values["scheduler_enabled"] == "true",
        "native_alert_delivery": values["native_alert_delivery"] == "true",
        "quiet_hours_start": dt.time.fromisoformat(values["quiet_hours_start"]),
        "quiet_hours_end": dt.time.fromisoformat(values["quiet_hours_end"]),
        "alert_daily_budget": int(values["alert_daily_budget"]),
        "alert_cooldown_minutes": int(values["alert_cooldown_minutes"]),
        "google_calendar_id": values["google_calendar_id"],
        "google_poll_minutes": int(values["google_poll_minutes"]),
        "caldav_url": values["caldav_url"],
        "caldav_calendar_name": values["caldav_calendar_name"] or None,
        "caldav_poll_minutes": int(values["caldav_poll_minutes"]),
        "ow_base_url": values["ow_base_url"],
        "ow_user_id": values["ow_user_id"] or None,
        "backup_provider": values["backup_provider"] or None,
    }
    if values["ow_api_key"]:
        updates["ow_api_key"] = SecretStr(values["ow_api_key"])
    if values["backup_passphrase"]:
        updates["backup_passphrase"] = SecretStr(values["backup_passphrase"])
    return updates


def _candidate_settings(values: dict[str, str], current: Settings) -> Settings:
    data = current.model_dump()
    data.update(_settings_updates(values, current))
    try:
        return Settings(_env_file=None, **data)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "invalid settings",
        ) from exc


def _update_dotenv(path: Path, updates: dict[str, str]) -> None:
    if path.is_symlink():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, ".env must not be a symlink")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            ".env must be a regular file",
        )
    serialized = {key: _dotenv_value(value) for key, value in updates.items()}
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(serialized)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.append("# Managed from the HealthMes web settings page.")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(output) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _dotenv_value(value: str) -> str:
    """Quote form input so it remains one exact dotenv value."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid setting value")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
