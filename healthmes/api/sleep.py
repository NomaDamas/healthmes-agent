from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from healthmes.api.auth import viewer_token, viewer_url
from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.local_session import (
    LocalBrowserSession,
    bootstrap_local_session,
    is_loopback_scope,
    issue_local_session,
    local_browser_url,
    require_local_session,
)
from healthmes.calendars import creds
from healthmes.calendars.approval import ApprovalCalendar, calendar_approval_target
from healthmes.calendars.base import CalendarError
from healthmes.calendars.connection import CalendarBackendFence
from healthmes.calendars.jobs import _build_backend, write_source
from healthmes.calendars.sleep_apply import (
    apply_sleep_proposal_from_observation,
    approval_token,
    decline_sleep_proposal,
)
from healthmes.calendars.sleep_proposals import (
    prepare_sleep_proposal_from_observation,
)
from healthmes.calendars.sleep_source import (
    SleepSummaryReader,
    read_actual_sleep,
)
from healthmes.config import Settings, resolve_timezone
from healthmes.mcp_server.ow_client import OWClient, OWClientError, resolve_single_user_id
from healthmes.store import SessionDep, SleepProposalStatus, SleepReconciliationProposal

router = APIRouter(tags=["sleep"])


class SleepReviewUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SleepReviewRuntime:
    reader: SleepSummaryReader
    user_id: str
    calendar: ApprovalCalendar | None
    backend_fence: CalendarBackendFence | None = None
    account_generation_resolver: Callable[[], str | None] | None = None
    calendar_target: str | None = None
    review_base_url: str | None = None
    review_url_builder: Callable[[dt.date], str] | None = None


@router.get("/sleep/unlock", response_class=HTMLResponse)
async def sleep_unlock_page(
    request: Request,
    proposal: uuid.UUID | None = None,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    if (
        not is_loopback_scope(request.scope)
        or not settings.api_token.get_secret_value().strip()
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "local browser required")
    path = (
        f"/sleep/unlock?proposal={proposal}"
        if proposal is not None
        else "/sleep/unlock"
    )
    template = template_environment().get_template("ui/local_unlock.html.j2")
    html = template.render(
        heading="Calendar 승인 잠금 해제",
        description="전체 API 토큰은 이 Mac의 HealthMes에만 전송됩니다.",
        post_url=local_browser_url(settings.port, path),
        active_nav="sleep",
        **shell_context(settings),
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"},
    )


@router.post("/sleep/unlock")
async def unlock_sleep_page(
    request: Request,
    proposal: uuid.UUID | None = None,
) -> RedirectResponse:
    location = f"/sleep?proposal={proposal}" if proposal is not None else "/sleep"
    response = RedirectResponse(location, status_code=303)
    await bootstrap_local_session(request, response)
    return response


@router.get("/sleep", response_class=HTMLResponse)
async def sleep_review_page(
    request: Request,
    response: Response,
    session: SessionDep,
    date: dt.date | None = None,
    proposal: uuid.UUID | None = None,
) -> HTMLResponse:
    local = issue_local_session(request, response)
    settings: Settings = request.app.state.settings
    target_date = date or dt.datetime.now(resolve_timezone(settings)).date()
    error: str | None = None
    record: SleepReconciliationProposal | None = None
    try:
        if proposal is not None:
            record = session.get(SleepReconciliationProposal, proposal)
        elif local is not None:
            runtime = await _runtime(request, settings)
            selected = await read_actual_sleep(
                runtime.reader,
                runtime.user_id,
                target_date,
                review_base_url=_review_base_url(runtime),
                review_url_builder=_review_url_builder(runtime),
            )
            with _calendar_runtime(runtime, session) as calendar:
                record = prepare_sleep_proposal_from_observation(
                    target_date=target_date,
                    calendar_source=calendar.backend.source,
                    selected=selected,
                    session=session,
                    calendar=calendar,
                )
    except (CalendarError, LookupError, OWClientError, SleepReviewUnavailable) as exc:
        error = _safe_error(exc)
    html = _render(
        settings,
        record,
        local,
        error,
        request.app.state.local_sessions.signing_secret,
        local_unlock_url=_local_unlock_url(settings, record) if local is None else "",
    )
    rendered = HTMLResponse(html, headers=response.headers)
    return rendered


@router.post("/sleep/apply")
async def apply_sleep(request: Request, session: SessionDep) -> RedirectResponse:
    form = await _form(request)
    local = require_local_session(request, csrf_token=form.get("csrf", ""))
    proposal_id = _proposal_id(form)
    settings: Settings = request.app.state.settings
    runtime = await _runtime(request, settings)
    store = request.app.state.local_sessions
    proposal = session.get(SleepReconciliationProposal, proposal_id)
    if proposal is not None:
        selected = await read_actual_sleep(
            runtime.reader,
            runtime.user_id,
            proposal.local_date,
            review_base_url=_review_base_url(runtime),
            review_url_builder=_review_url_builder(runtime),
        )
        with _calendar_runtime(runtime, session) as calendar:
            apply_sleep_proposal_from_observation(
                proposal_id=proposal_id,
                submitted_token=form.get("approval", ""),
                local_session_id=local.session_id,
                secret=store.signing_secret,
                selected=selected,
                session=session,
                calendar=calendar,
            )
    return RedirectResponse(f"/sleep?proposal={proposal_id}", status_code=303)


@router.post("/sleep/keep")
async def keep_calendar(request: Request, session: SessionDep) -> RedirectResponse:
    form = await _form(request)
    require_local_session(request, csrf_token=form.get("csrf", ""))
    proposal_id = _proposal_id(form)
    decline_sleep_proposal(session, proposal_id)
    return RedirectResponse(f"/sleep?proposal={proposal_id}", status_code=303)


async def _runtime(request: Request, settings: Settings) -> SleepReviewRuntime:
    injected = getattr(request.app.state, "sleep_review_runtime", None)
    if isinstance(injected, SleepReviewRuntime):
        return injected
    source = write_source(settings)
    if source is None:
        raise SleepReviewUnavailable("Google 또는 iCloud Calendar 연결이 필요합니다.")
    reader = OWClient.from_settings(settings)
    user_id = await resolve_single_user_id(reader, settings)
    return SleepReviewRuntime(
        reader,
        user_id,
        None,
        backend_fence=CalendarBackendFence(
            source=source,
            backend_factory=lambda: _build_backend(settings, source),
            generation_resolver=lambda: (
                creds.calendar_connection_generation(settings, source)
            ),
        ),
        account_generation_resolver=lambda: (
            creds.calendar_account_generation(settings, source)
        ),
        calendar_target=calendar_approval_target(settings, source),
        review_base_url=settings.public_base_url,
        review_url_builder=lambda target_date: viewer_url(
            settings,
            f"/sleep?date={target_date.isoformat()}",
        ),
    )


@contextmanager
def _calendar_runtime(
    runtime: SleepReviewRuntime,
    session: SessionDep,
) -> Iterator[ApprovalCalendar]:
    if runtime.backend_fence is not None:
        if runtime.calendar_target is None:
            raise SleepReviewUnavailable(
                "Calendar runtime target is unavailable."
            )
        with runtime.backend_fence.use(session) as backend:
            account_generation = (
                runtime.account_generation_resolver()
                if runtime.account_generation_resolver is not None
                else None
            )
            if (
                runtime.account_generation_resolver is not None
                and account_generation is None
            ):
                raise SleepReviewUnavailable(
                    "Calendar account identity is unavailable."
                )
            yield ApprovalCalendar(
                backend,
                runtime.calendar_target,
                runtime.review_base_url,
                runtime.review_url_builder,
                account_generation,
            )
        return
    if runtime.calendar is not None:
        yield runtime.calendar
        return
    raise SleepReviewUnavailable(
        "Calendar runtime is unavailable."
    )


def _review_base_url(runtime: SleepReviewRuntime) -> str | None:
    if runtime.review_base_url is not None:
        return runtime.review_base_url
    if runtime.calendar is not None:
        return runtime.calendar.review_base_url
    return None


def _review_url_builder(
    runtime: SleepReviewRuntime,
) -> Callable[[dt.date], str] | None:
    if runtime.review_url_builder is not None:
        return runtime.review_url_builder
    if runtime.calendar is not None:
        return runtime.calendar.review_url_builder
    return None


def _local_unlock_url(
    settings: Settings,
    record: SleepReconciliationProposal | None,
) -> str:
    api_token = settings.api_token.get_secret_value().strip()
    if not api_token:
        return ""
    path = (
        f"/sleep/unlock?proposal={record.id}"
        if record is not None
        else "/sleep/unlock"
    )
    separator = "&" if "?" in path else "?"
    return local_browser_url(
        settings.port,
        f"{path}{separator}token={viewer_token(api_token)}",
    )


def _render(
    settings: Settings,
    proposal: SleepReconciliationProposal | None,
    local: LocalBrowserSession | None,
    error: str | None,
    signing_secret: bytes,
    *,
    local_unlock_url: str,
) -> str:
    token = ""
    display_start = None
    display_wake = None
    receipt_start = None
    receipt_wake = None
    display_segments: list[dict[str, object]] = []
    receipt_segments: list[dict[str, str]] = []
    if proposal is not None and local is not None:
        token = approval_token(
            proposal,
            local.session_id,
            signing_secret,
        )
    if proposal is not None:
        try:
            timezone = resolve_timezone(settings)
            if proposal.snapshot.get("start"):
                display_start = _display_timestamp(
                    str(proposal.snapshot["start"]),
                    timezone,
                )
            if proposal.snapshot.get("wake_time"):
                display_wake = _display_timestamp(
                    str(proposal.snapshot["wake_time"]),
                    timezone,
                )
            for index, segment in enumerate(
                proposal.snapshot.get("segments", []),
                start=1,
            ):
                if not isinstance(segment, dict):
                    continue
                display_segments.append(
                    {
                        "index": index,
                        "start": _display_timestamp(str(segment["start"]), timezone),
                        "wake_time": _display_timestamp(
                            str(segment["wake_time"]),
                            timezone,
                        ),
                        "duration_minutes": segment["duration_minutes"],
                    }
                )
            if proposal.receipt:
                receipt_start = _display_timestamp(
                    str(proposal.receipt["start"]),
                    timezone,
                )
                receipt_wake = _display_timestamp(
                    str(proposal.receipt["wake_time"]),
                    timezone,
                )
                for segment in proposal.receipt.get("segments", []):
                    if not isinstance(segment, dict):
                        continue
                    receipt_segments.append(
                        {
                            "start": _display_timestamp(str(segment["start"]), timezone),
                            "wake_time": _display_timestamp(
                                str(segment["wake_time"]),
                                timezone,
                            ),
                        }
                    )
        except (AttributeError, KeyError, TypeError, ValueError):
            error = "저장된 수면 preview를 표시할 수 없습니다."
    template = template_environment().get_template("ui/sleep.html.j2")
    return template.render(
        proposal=proposal,
        local_session=local,
        approval_token=token,
        display_start=display_start,
        display_wake=display_wake,
        display_segments=display_segments,
        receipt_start=receipt_start,
        receipt_wake=receipt_wake,
        receipt_segments=receipt_segments,
        error=error,
        pending=proposal is not None and proposal.status is SleepProposalStatus.PENDING,
        local_unlock_url=local_unlock_url,
        active_nav="sleep",
        **shell_context(settings),
    )


def _display_timestamp(value: str, timezone: dt.tzinfo) -> str:
    return dt.datetime.fromisoformat(value).astimezone(timezone).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


async def _form(request: Request) -> dict[str, str]:
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {key: rows[-1] for key, rows in values.items() if rows}


def _proposal_id(form: dict[str, str]) -> uuid.UUID:
    try:
        return uuid.UUID(form.get("proposal_id", ""))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid proposal id") from exc


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SleepReviewUnavailable):
        return str(exc)
    return f"상태 확인 실패 ({type(exc).__name__})"
