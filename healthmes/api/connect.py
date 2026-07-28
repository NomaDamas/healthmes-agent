from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from healthmes.api.connection_status import (
    ConnectionCard,
    build_connection_cards,
    build_oura_card,
)
from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.local_session import issue_local_session
from healthmes.config import Settings

__all__ = ["router", "build_connection_cards", "build_oura_card", "ConnectionCard"]

router = APIRouter(tags=["connect"])


@router.get("/connect", response_class=HTMLResponse)
async def connect_status_page(request: Request, response: Response) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    local = issue_local_session(request, response)
    template = template_environment().get_template("ui/connect.html.j2")
    html = template.render(
        cards=[await build_oura_card(settings), *build_connection_cards(settings)],
        scheduler_enabled=settings.scheduler_enabled,
        local_session=local,
        google_result=request.query_params.get("google", ""),
        active_nav="connect",
        **shell_context(settings),
    )
    return HTMLResponse(html, headers=response.headers)
