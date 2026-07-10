"""Bearer-token authentication for the whole HealthMes HTTP surface.

docs/PLAN.md §9 mandates that medical data never leaves this machine, yet the
service is designed to be network-reachable (the Android usage collector
POSTs over LAN, Telegram alert links open in the phone browser). The
reconciliation is a single shared bearer token (``Settings.api_token``):

- When configured, :class:`BearerTokenMiddleware` requires
  ``Authorization: Bearer <token>`` on **every** request — all ``/v1``
  routers, the bare plan-verbatim paths, and ``POST /mcp``. The Android
  collector already sends this header (apps/android-usage .../IngestClient.kt).
- ``GET /health`` stays open (compose healthcheck / liveness probe; it leaks
  nothing).
- Human-facing viewer pages (``GET /decisions...``, the weekly report under
  ``GET /reports/...`` and the vendored ``/static/mermaid.min.js`` they load)
  additionally accept ``?token=<viewer token>`` where the viewer token is
  *derived* from the API token (:func:`viewer_token`). Alert/briefing links
  must be tappable from a phone browser, which cannot attach headers —
  embedding the derived read-only credential keeps links working without ever
  putting the full-access API token into Telegram messages or browser history.
- When no token is configured the middleware is not installed (the zero-setup
  loopback dev path); ``python -m healthmes serve`` refuses to bind a
  non-loopback host in that state (see ``healthmes/__main__.py``).

Implemented as pure ASGI (not ``BaseHTTPMiddleware``) so the /mcp
Streamable-HTTP responses keep streaming untouched.
"""

import hashlib
import hmac
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from healthmes.api.errors import error_body
from healthmes.config import Settings

__all__ = [
    "BearerTokenMiddleware",
    "install_auth",
    "viewer_token",
    "viewer_url",
]

# Paths that must stay reachable without credentials (liveness only).
OPEN_PATHS = frozenset({"/health"})

# Path prefixes of the human-facing viewer surface that may authenticate via
# the derived ?token= query credential (browser links cannot carry headers).
VIEWER_PATH_PREFIXES = ("/decisions", "/static/", "/reports")

_VIEWER_TOKEN_CONTEXT = b"healthmes-viewer:"


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
        url = f"{url}?token={viewer_token(api_token)}"
    return url


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


class BearerTokenMiddleware:
    """Rejects unauthenticated requests with the standard 401 error envelope."""

    def __init__(self, app: ASGIApp, api_token: str) -> None:
        if not api_token:
            raise ValueError("BearerTokenMiddleware requires a non-empty token")
        self._app = app
        self._token = api_token
        self._viewer_token = viewer_token(api_token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_authorized(scope):
            await self._app(scope, receive, send)
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
        authorization = _header(scope, b"authorization")
        if authorization is not None:
            prefix, _, credential = authorization.partition(" ")
            if prefix.lower() == "bearer" and hmac.compare_digest(
                credential.strip(), self._token
            ):
                return True
        if scope.get("method") in ("GET", "HEAD") and path.startswith(VIEWER_PATH_PREFIXES):
            return self._query_token_ok(scope)
        return False

    def _query_token_ok(self, scope: Scope) -> bool:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        for candidate in query.get("token", ()):
            # The viewer token is the linkable credential; the full API token
            # is accepted too so a power user can paste it manually.
            if hmac.compare_digest(candidate, self._viewer_token) or hmac.compare_digest(
                candidate, self._token
            ):
                return True
        return False


def install_auth(app, settings: Settings) -> bool:
    """Install the bearer middleware when a token is configured.

    Returns True when auth is active. Called by the app factory — a single
    composition point so REST, viewer pages and /mcp are all covered by the
    same gate.
    """
    token = settings.api_token.get_secret_value().strip()
    if not token:
        return False
    app.add_middleware(BearerTokenMiddleware, api_token=token)
    return True
