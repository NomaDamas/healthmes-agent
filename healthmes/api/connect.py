from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from healthmes.api.auth import viewer_token
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
from healthmes.api.wearables import (
    build_wearable_hub,
    fixed_oauth_return_url,
    get_management_client,
)
from healthmes.config import Settings
from healthmes.wearables.management import (
    WearableManagementCapabilityError,
    normalize_management_provider_identifier,
)

__all__ = ["router", "build_connection_cards", "build_oura_card", "ConnectionCard"]

router = APIRouter(tags=["connect"])


def _normalize_wearable_provider(provider: str) -> str:
    try:
        return normalize_management_provider_identifier(provider)
    except (ValueError, WearableManagementCapabilityError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "invalid wearable provider",
        ) from exc


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
    wearable_client = getattr(
        request.app.state,
        "wearable_management_client",
        None,
    )
    template = template_environment().get_template("ui/connect.html.j2")
    html = template.render(
        cards=[await build_oura_card(settings), *build_connection_cards(settings)],
        wearable_hub=await build_wearable_hub(
            settings,
            client=wearable_client,
        ),
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
        wearable_result=request.query_params.get("wearable", ""),
        wearable_status=request.query_params.get("status", ""),
        active_nav="connect",
        **shell_context(settings),
    )
    return HTMLResponse(html, headers=response.headers)


@router.get("/connect/wearables/oauth-complete")
async def wearable_oauth_complete(request: Request) -> RedirectResponse:
    provider = _normalize_wearable_provider(
        request.query_params.get("provider", ""),
    )
    settings: Settings = request.app.state.settings
    oauth_error = request.query_params.get("error", "").strip()
    connection_active = False
    if not oauth_error:
        try:
            client = await get_management_client(request)
            connection_active = any(
                row.get("provider") == provider
                and row.get("status") == "active"
                for row in await client.connections()
            )
        except Exception:
            # Do not claim success when the upstream state cannot be verified.
            connection_active = False
    params = {
        "wearable": provider,
        "status": (
            "authorization_failed"
            if oauth_error
            else "connected"
            if connection_active
            else "reconnect_required"
        ),
    }
    api_token = settings.api_token.get_secret_value().strip()
    if api_token:
        params["token"] = viewer_token(api_token)
    return RedirectResponse(
        f"/connect?{urlencode(params)}",
        status_code=303,
    )


async def _require_wearable_local_session(request: Request) -> None:
    form = await request.form()
    csrf = form.get("csrf")
    require_local_session(
        request,
        csrf_token=csrf if isinstance(csrf, str) else "",
    )


def _wearable_redirect(provider: str, status: str) -> RedirectResponse:
    return RedirectResponse(
        f"/connect?wearable={provider}&status={status}",
        status_code=303,
    )


@router.post("/connect/wearables/{provider}/authorize")
async def start_wearable_authorization(
    provider: str,
    request: Request,
) -> RedirectResponse:
    await _require_wearable_local_session(request)
    normalized = _normalize_wearable_provider(provider)
    try:
        settings: Settings = request.app.state.settings
        client = await get_management_client(request)
        result = await client.authorize_url(
            normalized,
            redirect_uri=fixed_oauth_return_url(settings, normalized),
        )
    except Exception:
        return _wearable_redirect(normalized, "error")
    return RedirectResponse(result["authorization_url"], status_code=303)


@router.post("/connect/wearables/{provider}/disconnect")
async def disconnect_wearable(
    provider: str,
    request: Request,
) -> RedirectResponse:
    await _require_wearable_local_session(request)
    normalized = _normalize_wearable_provider(provider)
    try:
        await (await get_management_client(request)).disconnect(normalized)
    except Exception:
        return _wearable_redirect(normalized, "error")
    return _wearable_redirect(normalized, "disconnected")


@router.post("/connect/wearables/{provider}/sync")
async def sync_wearable(
    provider: str,
    request: Request,
) -> RedirectResponse:
    await _require_wearable_local_session(request)
    normalized = _normalize_wearable_provider(provider)
    try:
        await (await get_management_client(request)).sync(normalized)
    except Exception:
        return _wearable_redirect(normalized, "error")
    return _wearable_redirect(normalized, "sync_queued")


@router.post("/connect/wearables/{provider}/sync/historical")
async def historical_sync_wearable(
    provider: str,
    request: Request,
) -> RedirectResponse:
    await _require_wearable_local_session(request)
    normalized = _normalize_wearable_provider(provider)
    form = await request.form()
    raw_days = form.get("days", "90")
    try:
        days = int(raw_days) if isinstance(raw_days, str) else 90
    except ValueError:
        days = 0
    if not 1 <= days <= 365:
        return _wearable_redirect(normalized, "invalid_days")
    try:
        await (await get_management_client(request)).historical_sync(
            normalized,
            days=days,
        )
    except Exception:
        return _wearable_redirect(normalized, "error")
    return _wearable_redirect(normalized, "history_queued")
