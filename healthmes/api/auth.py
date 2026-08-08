"""Bearer-token authentication for the whole HealthMes HTTP surface.

docs/PLAN.md §9 mandates that medical data never leaves this machine, yet the
service is designed to be network-reachable (the Android usage collector
POSTs over LAN, Telegram alert links open in the phone browser). The
reconciliation is a single shared bearer token (``Settings.api_token``):

- When configured, :class:`BearerTokenMiddleware` requires
  ``Authorization: Bearer <token>`` on protected requests — all ``/v1``
  routers, the bare plan-verbatim paths, and ``POST /mcp``. The Android
  collector already sends this header (apps/android-usage .../IngestClient.kt).
- ``GET /health``, the static ``GET /`` landing, and the credential-checking
  ``/unlock`` form stay open. None of them reads protected health data.
- Human-facing viewer pages (``GET /decisions...``, the weekly report under
  ``GET /reports/...``, the vendored ``/static/mermaid.min.js`` they load, and
  the stored media files under ``GET /v1/media/...`` those pages embed)
  additionally accept ``?token=<viewer token>`` where the viewer token is
  *derived* from the API token (:func:`viewer_token`). Alert/briefing links
  must be tappable from a phone browser, which cannot attach headers —
  embedding the derived read-only credential keeps links working without ever
  putting the full-access API token into Telegram messages or browser history.
- Loopback-only Calendar write flows accept a short-lived local session only
  after a full API credential bootstraps it. A loopback proxy connection and
  attacker-controlled ``Host`` header alone never grant that session.
- When no token is configured the middleware is not installed (the zero-setup
  loopback dev path); ``python -m healthmes serve`` refuses to bind a
  non-loopback host in that state (see ``healthmes/__main__.py``).

Implemented as pure ASGI (not ``BaseHTTPMiddleware``) so the /mcp
Streamable-HTTP responses keep streaming untouched.
"""

import hashlib
import hmac
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from healthmes.api.errors import error_body
from healthmes.api.local_session import (
    LOCAL_SESSION_AUTH_SCOPE_KEY,
    LocalSessionStore,
    authenticated_local_session,
)
from healthmes.config import Settings

__all__ = [
    "BearerTokenMiddleware",
    "HUMAN_VIEWER_PATH_PREFIXES",
    "install_auth",
    "is_human_viewer_path",
    "viewer_token",
    "viewer_url",
]

# Paths that must stay reachable without credentials. "/health" is liveness;
# "/" is the static landing shell — it renders links only (no data, no
# credentials in markup; healthmes/api/decisions.py::landing), so exposing it
# leaks nothing while giving humans an entry point on the public host.
# "/unlock" validates a submitted API token itself, then redirects with only
# the derived read-only viewer credential.
OPEN_PATHS = frozenset({"/health", "/", "/unlock"})
OPEN_POST_PATHS = frozenset({"/v1/setup/pairing/exchange"})

# Path prefixes of the human-facing viewer surface that may authenticate via
# the derived ?token= query credential (browser links cannot carry headers).
# "/v1/media/" is the one /v1 namespace included — read-only media serving
# (GET/HEAD only; the middleware never applies the query credential to other
# methods), so decision/report pages and in-app web views can embed captured
# photos/voice notes via <img>/<audio> tags. Uploading (POST /v1/media, no
# trailing slash — not matched by the prefix) stays bearer-only. "/connect"
# is the read-only calendar-connection status page (healthmes/api/connect.py
# — status + instructions, no secrets rendered, and viewer credentials apply
# only to GET/HEAD. Storage follows the same read-only rule; its writes still
# require a loopback local session and CSRF token.
VIEWER_PATH_PREFIXES = (
    "/dashboard",
    "/decisions",
    "/static/",
    "/reports",
    "/v1/media/",
    "/connect",
    "/sleep",
    "/storage",
)
HUMAN_VIEWER_PATH_PREFIXES = (
    "/dashboard",
    "/decisions",
    "/reports",
    "/connect",
    "/sleep",
    "/storage",
)
LOCAL_SESSION_BOOTSTRAP_POST_PATHS = frozenset(
    {"/connect/unlock", "/sleep/unlock"}
)
_VIEWER_TOKEN_CONTEXT = b"healthmes-viewer:"


def _matches_path_prefix(path: str, prefix: str) -> bool:
    """Match a route prefix without granting similarly named sibling routes."""
    return path == prefix or (
        prefix.endswith("/") and path.startswith(prefix)
    ) or path.startswith(f"{prefix}/")


def viewer_token(api_token: str) -> str:
    """Derived read-only credential embedded in decision-viewer links.

    Deterministic function of the API token, so links stay valid across
    restarts; knowing it grants access to the viewer pages only, never to the
    REST/MCP surface. Rotating the API token rotates it.
    """
    digest = hashlib.sha256(_VIEWER_TOKEN_CONTEXT + api_token.encode("utf-8"))
    return digest.hexdigest()[:32]


def viewer_url(settings: Settings, path: str) -> str:
    """Absolute browser-tappable link to a viewer-surface page.

    The single construction point for every credentialed viewer link the
    system emits — decision pages (REST + the MCP ``record_decision`` tool),
    glance alert deep links, and the weekly report: ``{public_base_url}``
    ``{path}`` plus ``?token=`` from :func:`viewer_token` when an API token is
    configured. Links open in a phone browser, which cannot attach
    Authorization headers, and must never carry the full-access API token —
    server code builds these links, never the LLM (one copy here so the
    credential scheme can only evolve in lockstep).
    """
    url = f"{settings.public_base_url.rstrip('/')}{path}"
    api_token = settings.api_token.get_secret_value().strip()
    if api_token:
        parts = urlsplit(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "token"
        ]
        query.append(("token", viewer_token(api_token)))
        url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )
    return url


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


class BearerTokenMiddleware:
    """Reject APIs with JSON 401 and human viewer pages with a friendly HTML 401."""

    def __init__(
        self,
        app: ASGIApp,
        api_token: str,
        local_sessions: LocalSessionStore,
        settings: Settings,
    ) -> None:
        if not api_token:
            raise ValueError("BearerTokenMiddleware requires a non-empty token")
        self._app = app
        self._token = api_token
        self._viewer_token = viewer_token(api_token)
        self._local_sessions = local_sessions
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_authorized(scope):
            await self._app(scope, receive, send)
            return
        if self._should_render_unlock(scope):
            response = self._viewer_unlock_response(scope)
            await response(scope, receive, send)
            return
        response = JSONResponse(
            status_code=401,
            content=error_body(
                "unauthorized",
                "Missing or invalid bearer token (send 'Authorization: Bearer <token>').",
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)

    # -- internals ---------------------------------------------------------

    def _is_authorized(self, scope: Scope) -> bool:
        path = scope.get("path", "")
        if path in OPEN_PATHS:
            return True
        if scope.get("method") == "POST" and path in OPEN_POST_PATHS:
            return True
        if (
            scope.get("method") == "POST"
            and path in LOCAL_SESSION_BOOTSTRAP_POST_PATHS
        ):
            return True
        authorization = _header(scope, b"authorization")
        if authorization is not None:
            prefix, _, credential = authorization.partition(" ")
            if prefix.lower() == "bearer" and hmac.compare_digest(
                credential.strip(), self._token
            ):
                self._mark_local_session_authenticated(scope)
                return True
        if self._is_local_browser_path(path):
            session = authenticated_local_session(scope, self._local_sessions)
            if session is not None:
                self._mark_local_session_authenticated(scope)
                return True
        if scope.get("method") in ("GET", "HEAD") and any(
            _matches_path_prefix(path, prefix) for prefix in VIEWER_PATH_PREFIXES
        ):
            return self._query_token_ok(scope)
        return False

    def _query_token_ok(self, scope: Scope) -> bool:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        for candidate in query.get("token", ()):
            if hmac.compare_digest(candidate, self._viewer_token):
                return True
        return False

    @staticmethod
    def _should_render_unlock(scope: Scope) -> bool:
        path = scope.get("path", "")
        return (
            scope.get("method") in ("GET", "HEAD")
            and is_human_viewer_path(path)
        )

    def _viewer_unlock_response(self, scope: Scope) -> HTMLResponse:
        # Imported lazily to avoid the auth -> dashboard -> auth import cycle.
        from healthmes.api.dashboard import render_viewer_unlock_html

        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode("latin-1")
        target = f"{path}?{query}" if query else path
        return HTMLResponse(
            render_viewer_unlock_html(self._settings, target),
            status_code=401,
            headers={
                "WWW-Authenticate": "Bearer",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @staticmethod
    def _is_local_browser_path(path: str) -> bool:
        return (
            path == "/connect"
            or path.startswith("/connect/google/")
            or path == "/sleep"
            or path.startswith("/sleep/")
            or path == "/storage"
            or path.startswith("/storage/")
        )

    @staticmethod
    def _mark_local_session_authenticated(scope: Scope) -> None:
        scope.setdefault("state", {})[LOCAL_SESSION_AUTH_SCOPE_KEY] = True


def install_auth(app, settings: Settings) -> bool:
    """Install the bearer middleware when a token is configured.

    Returns True when auth is active. Called by the app factory — a single
    composition point so REST, viewer pages and /mcp are all covered by the
    same gate.
    """
    token = settings.api_token.get_secret_value().strip()
    if not token:
        return False
    app.add_middleware(
        BearerTokenMiddleware,
        api_token=token,
        local_sessions=app.state.local_sessions,
        settings=settings,
    )
    return True


def is_human_viewer_path(path: str) -> bool:
    """Whether ``path`` is an HTML surface that should fail with friendly UX."""
    if path.endswith(".json"):
        return False
    return any(
        _matches_path_prefix(path, prefix)
        for prefix in HUMAN_VIEWER_PATH_PREFIXES
    )
