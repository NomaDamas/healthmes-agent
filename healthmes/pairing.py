"""Companion-app pairing payload (docs/PLAN.md §13 — QR onboarding).

The companion apps (and bridge apps like Health Auto Export) need exactly
two values: the instance base URL and the bearer token. ``healthmes connect
qr`` renders them as one scannable QR so nobody types a 64-char token on a
phone keyboard.

Payload format: ``healthmes://pair?url=<base>&token=<bearer>`` — the same
custom scheme the iOS/Android companions already register for deep links
(``apps/ios-companion/project.yml``); the ``pair`` route lands app-side with
the deferred HealthKit module work. Until then the QR's human-visible
fallback (printed next to it) is copy-paste friendly.

The token is embedded on purpose: pairing IS credential handoff. The QR is
drawn on the terminal of the machine that already owns the token and never
persisted, logged, or served over HTTP.
"""

from urllib.parse import quote

from healthmes.config import Settings


def build_pairing_url(settings: Settings) -> str:
    """The ``healthmes://pair`` deep link for this instance."""
    base = settings.public_base_url.rstrip("/")
    token = settings.api_token.get_secret_value().strip()
    url = f"healthmes://pair?url={quote(base, safe='')}"
    if token:
        url += f"&token={quote(token, safe='')}"
    return url


def render_terminal_qr(payload: str) -> str:
    """The payload as a compact terminal QR block (ANSI half-blocks)."""
    import io

    import segno

    qr = segno.make(payload, error="m")
    buffer = io.StringIO()
    qr.terminal(out=buffer, compact=True, border=2)
    return buffer.getvalue()
