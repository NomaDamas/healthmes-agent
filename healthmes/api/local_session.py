from __future__ import annotations

import datetime as dt
import ipaddress
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status

LOCAL_SESSION_COOKIE = "healthmes_local_session"
LOCAL_SESSION_TTL = dt.timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class LocalBrowserSession:
    session_id: str
    csrf_token: str
    expires_at: dt.datetime


class LocalSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, LocalBrowserSession] = {}
        self.signing_secret = secrets.token_bytes(32)

    def issue(self, now: dt.datetime | None = None) -> LocalBrowserSession:
        current = now or dt.datetime.now(dt.UTC)
        session = LocalBrowserSession(
            session_id=secrets.token_urlsafe(24),
            csrf_token=secrets.token_urlsafe(24),
            expires_at=current + LOCAL_SESSION_TTL,
        )
        self._sessions[session.session_id] = session
        return session

    def get(
        self,
        session_id: str | None,
        now: dt.datetime | None = None,
    ) -> LocalBrowserSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        current = now or dt.datetime.now(dt.UTC)
        if current >= session.expires_at:
            self._sessions.pop(session_id, None)
            return None
        return session

    def revoke(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


def install_local_sessions(app) -> None:
    app.state.local_sessions = LocalSessionStore()


def is_loopback_scope(scope: dict) -> bool:
    host = _host_name(_header(scope, b"host"))
    client = scope.get("client")
    client_host = client[0] if isinstance(client, tuple) and client else ""
    return _is_loopback(host) and (
        _is_loopback(client_host) or client_host == "testclient"
    )


def issue_local_session(request: Request, response: Response) -> LocalBrowserSession | None:
    if not is_loopback_scope(request.scope):
        return None
    store: LocalSessionStore = request.app.state.local_sessions
    session = store.get(request.cookies.get(LOCAL_SESSION_COOKIE)) or store.issue()
    response.set_cookie(
        LOCAL_SESSION_COOKIE,
        session.session_id,
        max_age=int(LOCAL_SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return session


def require_local_session(
    request: Request,
    *,
    csrf_token: str,
) -> LocalBrowserSession:
    if not is_loopback_scope(request.scope):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "local browser session required")
    _assert_same_origin(request)
    store: LocalSessionStore = request.app.state.local_sessions
    session = store.get(request.cookies.get(LOCAL_SESSION_COOKIE))
    if session is None or not secrets.compare_digest(session.csrf_token, csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid local session or CSRF token")
    return session


def _assert_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin and Host are required")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != host:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin does not match Host")


def _header(scope: dict, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return ""


def _host_name(host: str) -> str:
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if ":" in host else host


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"
