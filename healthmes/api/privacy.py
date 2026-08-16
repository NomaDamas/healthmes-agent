from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from healthmes.api.decision_html import shell_context, template_environment

router = APIRouter(tags=["privacy"])


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_page(request: Request) -> HTMLResponse:
    template = template_environment().get_template("ui/privacy.html.j2")
    return HTMLResponse(template.render(active_nav="", **shell_context(None)))
