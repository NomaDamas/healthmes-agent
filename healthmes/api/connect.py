from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

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
)
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
    template = template_environment().get_template("ui/connect.html.j2")
    html = template.render(
        cards=[await build_oura_card(settings), *build_connection_cards(settings)],
        scheduler_enabled=settings.scheduler_enabled,
        local_session=local,
        local_unlock_url=(
            local_browser_url(settings.port, "/connect/unlock")
            if (
                local is None
                and bool(settings.api_token.get_secret_value().strip())
                and is_loopback_scope(request.scope)
            )
            else ""
        ),
        google_result=request.query_params.get("google", ""),
        active_nav="connect",
        **shell_context(settings),
    )
    return HTMLResponse(html, headers=response.headers)
