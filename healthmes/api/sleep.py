from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.local_session import (
    LocalBrowserSession,
    issue_local_session,
    require_local_session,
)
from healthmes.calendars.base import CalendarBackend, CalendarError
from healthmes.calendars.jobs import _build_backend, write_source
from healthmes.calendars.sleep_apply import (
    apply_sleep_proposal,
    approval_token,
    decline_sleep_proposal,
)
from healthmes.calendars.sleep_proposals import prepare_sleep_proposal
from healthmes.calendars.sleep_source import SleepSummaryReader
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
    backend: CalendarBackend


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
        runtime = await _runtime(request, settings)
        record = (
            session.get(SleepReconciliationProposal, proposal)
            if proposal is not None
            else await prepare_sleep_proposal(
                target_date=target_date,
                calendar_source=runtime.backend.source,
                reader=runtime.reader,
                user_id=runtime.user_id,
                session=session,
                backend=runtime.backend,
            )
        )
    except (CalendarError, LookupError, OWClientError, SleepReviewUnavailable) as exc:
        error = _safe_error(exc)
    html = _render(
        settings,
        record,
        local,
        error,
        request.app.state.local_sessions.signing_secret,
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
    await apply_sleep_proposal(
        proposal_id=proposal_id,
        submitted_token=form.get("approval", ""),
        local_session_id=local.session_id,
        secret=store.signing_secret,
        reader=runtime.reader,
        user_id=runtime.user_id,
        session=session,
        backend=runtime.backend,
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
    return SleepReviewRuntime(reader, user_id, _build_backend(settings, source))


def _render(
    settings: Settings,
    proposal: SleepReconciliationProposal | None,
    local: LocalBrowserSession | None,
    error: str | None,
    signing_secret: bytes,
) -> str:
    token = ""
    if proposal is not None and local is not None:
        token = approval_token(
            proposal,
            local.session_id,
            signing_secret,
        )
    template = template_environment().get_template("ui/sleep.html.j2")
    return template.render(
        proposal=proposal,
        local_session=local,
        approval_token=token,
        error=error,
        pending=proposal is not None and proposal.status is SleepProposalStatus.PENDING,
        active_nav="sleep",
        **shell_context(settings),
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
