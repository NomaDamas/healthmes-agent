import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from healthmes.api.auth import viewer_token
from healthmes.api.calendar_runtime import refresh_calendar_jobs
from healthmes.api.connection_status import (
    ConnectionCard,
    build_connection_cards,
    build_oura_card,
)
from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.local_session import (
    bootstrap_local_session,
    is_loopback_scope,
    issue_local_session,
    local_browser_url,
    require_local_session,
)
from healthmes.api.sleep import _form
from healthmes.calendars import creds
from healthmes.calendars.base import CalendarError
from healthmes.config import Settings

__all__ = ["router", "build_connection_cards", "build_oura_card", "ConnectionCard"]

router = APIRouter(tags=["connect"])


@router.get("/connect/unlock", response_class=HTMLResponse)
async def connect_unlock_page(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    if (
        not is_loopback_scope(request.scope)
        or not settings.api_token.get_secret_value().strip()
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "local browser required")
    template = template_environment().get_template("ui/local_unlock.html.j2")
    html = template.render(
        heading="연결 관리 잠금 해제",
        description="전체 API 토큰은 이 Mac의 HealthMes에만 전송됩니다.",
        post_url=local_browser_url(settings.port, "/connect/unlock"),
        active_nav="connect",
        **shell_context(settings),
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"},
    )


@router.post("/connect/unlock")
async def unlock_connect_page(request: Request) -> RedirectResponse:
    response = RedirectResponse("/connect", status_code=303)
    await bootstrap_local_session(request, response)
    return response


@router.get("/connect", response_class=HTMLResponse)
async def connect_status_page(request: Request, response: Response) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    local = issue_local_session(request, response)
    api_token = settings.api_token.get_secret_value().strip()
    template = template_environment().get_template("ui/connect.html.j2")
    html = template.render(
        cards=[await build_oura_card(settings), *build_connection_cards(settings)],
        scheduler_enabled=settings.scheduler_enabled,
        local_session=local,
        local_unlock_url=(
            local_browser_url(
                settings.port,
                f"/connect/unlock?token={viewer_token(api_token)}",
            )
            if local is None and api_token
            else ""
        ),
        google_result=request.query_params.get("google", ""),
        icloud_result=request.query_params.get("icloud", ""),
        active_nav="connect",
        **shell_context(settings),
    )
    return HTMLResponse(html, headers=response.headers)


@router.post("/connect/icloud")
async def connect_icloud(request: Request) -> RedirectResponse:
    form = await _form(request)
    require_local_session(request, csrf_token=form.get("csrf", ""))
    username = form.get("username", "").strip()
    app_password = form.get("app_password", "")
    settings: Settings = request.app.state.settings
    if not username or not app_password:
        return RedirectResponse("/connect?icloud=missing", status_code=303)
    try:
        await anyio.to_thread.run_sync(
            lambda: creds.validate_caldav_connection(
                username=username,
                app_password=app_password,
                url=settings.caldav_url,
            )
        )
        creds.save_caldav_credentials(
            settings.data_dir,
            username=username,
            app_password=app_password,
            url=settings.caldav_url,
        )
        refresh_calendar_jobs(request.app)
    except CalendarError:
        return RedirectResponse("/connect?icloud=failed", status_code=303)
    return RedirectResponse("/connect?icloud=connected", status_code=303)


@router.post("/connect/icloud/disconnect")
async def disconnect_icloud(request: Request) -> RedirectResponse:
    form = await _form(request)
    require_local_session(request, csrf_token=form.get("csrf", ""))
    settings: Settings = request.app.state.settings
    resolved = creds.resolve_caldav_credentials(settings)
    if resolved is not None and resolved.source == "env":
        return RedirectResponse("/connect?icloud=managed_by_env", status_code=303)
    creds.delete_caldav_credentials(settings.data_dir)
    refresh_calendar_jobs(request.app)
    return RedirectResponse("/connect?icloud=disconnected", status_code=303)
