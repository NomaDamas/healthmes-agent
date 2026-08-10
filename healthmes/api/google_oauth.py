from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from healthmes.api.calendar_runtime import refresh_calendar_jobs
from healthmes.api.local_session import (
    LocalBrowserSession,
    authenticated_local_session,
    require_local_session,
)
from healthmes.api.sleep import _form
from healthmes.calendars import creds
from healthmes.calendars.google import GOOGLE_SCOPES, google_token_path, save_credentials
from healthmes.calendars.google_client import resolve_google_client_secret
from healthmes.config import Settings

router = APIRouter(tags=["connect"])
OAUTH_TTL = dt.timedelta(minutes=10)


class OAuthCredentials(Protocol):
    def to_json(self) -> str: ...


class WebOAuthFlow(Protocol):
    credentials: OAuthCredentials

    def authorization_url(self, **kwargs) -> tuple[str, str]: ...
    def fetch_token(self, *, authorization_response: str) -> None: ...


@dataclass(frozen=True, slots=True)
class GoogleOAuthAttempt:
    local_session_id: str
    flow: WebOAuthFlow
    expires_at: dt.datetime


class GoogleOAuthStore:
    def __init__(self) -> None:
        self._attempts: dict[str, GoogleOAuthAttempt] = {}

    def put(self, state: str, attempt: GoogleOAuthAttempt) -> None:
        self._attempts[state] = attempt

    def take(
        self,
        state: str,
        local_session: LocalBrowserSession,
    ) -> GoogleOAuthAttempt | None:
        attempt = self._attempts.pop(state, None)
        if attempt is None:
            return None
        if (
            attempt.local_session_id != local_session.session_id
            or dt.datetime.now(dt.UTC) >= attempt.expires_at
        ):
            return None
        return attempt


def install_google_oauth(app) -> None:
    app.state.google_oauth = GoogleOAuthStore()


@router.post("/connect/google/start")
async def start_google_oauth(request: Request) -> RedirectResponse:
    local = await _authorized_local(request)
    return _start_redirect(request, local)


def _start_redirect(
    request: Request,
    local: LocalBrowserSession,
) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    client_secret = resolve_google_client_secret(settings)
    if client_secret is None:
        return RedirectResponse("/connect?google=client-secret-missing", status_code=303)
    redirect_uri = str(request.url_for("google_oauth_callback"))
    flow = _build_flow(request, client_secret, redirect_uri)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=secrets.token_urlsafe(24),
    )
    request.app.state.google_oauth.put(
        state,
        GoogleOAuthAttempt(
            local.session_id,
            flow,
            dt.datetime.now(dt.UTC) + OAUTH_TTL,
        ),
    )
    return RedirectResponse(authorization_url, status_code=303)


@router.get("/connect/google/callback", name="google_oauth_callback")
async def google_oauth_callback(request: Request, state: str = "") -> RedirectResponse:
    store = request.app.state.local_sessions
    local = authenticated_local_session(request.scope, store)
    if local is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "local browser session required")
    attempt = request.app.state.google_oauth.take(state, local)
    if attempt is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired OAuth state")
    flow = attempt.flow
    flow.fetch_token(authorization_response=str(request.url))
    settings: Settings = request.app.state.settings
    save_credentials(flow.credentials, google_token_path(settings.data_dir))
    refresh_calendar_jobs(request.app)
    return RedirectResponse("/connect?google=connected", status_code=303)


@router.post("/connect/google/reconnect")
async def reconnect_google(request: Request) -> RedirectResponse:
    local = await _authorized_local(request)
    return _start_redirect(request, local)


@router.post("/connect/google/disconnect")
async def disconnect_google(request: Request) -> RedirectResponse:
    await _authorized_local(request)
    settings: Settings = request.app.state.settings
    creds.delete_google_token(settings.data_dir)
    refresh_calendar_jobs(request.app)
    return RedirectResponse("/connect?google=disconnected", status_code=303)


async def _authorized_local(request: Request) -> LocalBrowserSession:
    form = await _form(request)
    return require_local_session(request, csrf_token=form.get("csrf", ""))


def _build_flow(
    request: Request,
    client_secret: Path,
    redirect_uri: str,
) -> WebOAuthFlow:
    factory = getattr(request.app.state, "google_oauth_flow_factory", None)
    if factory is not None:
        return factory(client_secret, GOOGLE_SCOPES, redirect_uri)
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(client_secret), scopes=list(GOOGLE_SCOPES))
    flow.redirect_uri = redirect_uri
    return flow
