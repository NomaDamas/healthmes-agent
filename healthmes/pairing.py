"""Short-lived, one-time companion-app pairing grants.

The QR/deep link carries the instance URL plus an opaque signed grant. It
never carries the long-lived API bearer token. The grant is:

- HMAC signed with the instance API token;
- expired after a short TTL;
- registered as a mode-0600 file under ``data_dir``;
- atomically claimed exactly once during exchange.

This keeps the normal pairing flow copy-free without leaving the API token in
screenshots, URL histories, QR payload logs, or deep-link forwarding records.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from healthmes.config import Settings

PAIRING_GRANT_TTL_SECONDS = 300
_PAIRING_CONTEXT = b"healthmes-pairing-v1:"


class PairingGrantError(ValueError):
    """Base class for invalid pairing grants."""


class PairingGrantExpired(PairingGrantError):
    """The pairing grant is no longer within its validity window."""


class PairingGrantConsumed(PairingGrantError):
    """The pairing grant was already used or was never registered."""


@dataclass(frozen=True, slots=True)
class PairingGrant:
    deep_link: str
    expires_at: int


def issue_pairing_grant(
    settings: Settings,
    *,
    now: int | None = None,
    ttl_seconds: int = PAIRING_GRANT_TTL_SECONDS,
) -> PairingGrant:
    """Create one signed, expiring, one-time pairing deep link."""
    api_token = settings.api_token.get_secret_value().strip()
    if not api_token:
        raise PairingGrantError(
            "pairing requires HEALTHMES_API_TOKEN; token-less loopback instances "
            "cannot be paired to another device"
        )
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + ttl_seconds
    nonce = secrets.token_urlsafe(24)
    payload = _encode_payload({"nonce": nonce, "exp": expires_at})
    signature = _sign(payload, api_token)
    code = f"{payload}.{signature}"

    grant_dir = _grant_dir(settings.data_dir)
    grant_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        grant_dir.chmod(0o700)
    except OSError:
        pass
    path = grant_dir / f"{nonce}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "nonce": nonce,
                    "expires_at": expires_at,
                    "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                },
                stream,
                separators=(",", ":"),
                sort_keys=True,
            )
    except BaseException:
        path.unlink(missing_ok=True)
        raise

    base = settings.public_base_url.rstrip("/")
    deep_link = (
        f"healthmes://pair?url={quote(base, safe='')}"
        f"&code={quote(code, safe='')}"
    )
    return PairingGrant(deep_link=deep_link, expires_at=expires_at)


def exchange_pairing_grant(
    settings: Settings,
    code: str,
    *,
    now: int | None = None,
) -> str:
    """Claim ``code`` once and return the instance bearer token."""
    api_token = settings.api_token.get_secret_value().strip()
    if not api_token:
        raise PairingGrantError("pairing is unavailable without an API token")
    payload, separator, signature = code.partition(".")
    if not separator or not payload or not signature:
        raise PairingGrantError("invalid pairing code")
    if not hmac.compare_digest(signature, _sign(payload, api_token)):
        raise PairingGrantError("invalid pairing code signature")
    claims = _decode_payload(payload)
    nonce = claims.get("nonce")
    expires_at = claims.get("exp")
    if not isinstance(nonce, str) or not nonce or not isinstance(expires_at, int):
        raise PairingGrantError("invalid pairing code claims")

    current = int(time.time() if now is None else now)
    if current > expires_at:
        _grant_path(settings.data_dir, nonce).unlink(missing_ok=True)
        raise PairingGrantExpired("pairing code expired")

    source = _grant_path(settings.data_dir, nonce)
    claimed = source.with_name(f".{nonce}.{secrets.token_hex(8)}.claimed")
    try:
        os.replace(source, claimed)
    except FileNotFoundError as exc:
        raise PairingGrantConsumed("pairing code already used") from exc
    try:
        record = json.loads(claimed.read_text(encoding="utf-8"))
        expected_hash = hashlib.sha256(code.encode()).hexdigest()
        if (
            record.get("nonce") != nonce
            or record.get("expires_at") != expires_at
            or not hmac.compare_digest(
                str(record.get("code_sha256", "")), expected_hash
            )
        ):
            raise PairingGrantError("pairing grant registry mismatch")
        if current > int(record["expires_at"]):
            raise PairingGrantExpired("pairing code expired")
        return api_token
    finally:
        claimed.unlink(missing_ok=True)


def build_pairing_url(settings: Settings) -> str:
    """Compatibility wrapper returning a new one-time pairing deep link."""
    return issue_pairing_grant(settings).deep_link


def render_terminal_qr(payload: str) -> str:
    """Render the payload as a compact terminal QR block."""
    import io

    import segno

    qr = segno.make(payload, error="m")
    buffer = io.StringIO()
    qr.terminal(out=buffer, compact=True, border=2)
    return buffer.getvalue()


def _grant_dir(data_dir: Path) -> Path:
    return data_dir / "pairing-grants"


def _grant_path(data_dir: Path, nonce: str) -> Path:
    if "/" in nonce or "\\" in nonce or nonce in {".", ".."}:
        raise PairingGrantError("invalid pairing nonce")
    return _grant_dir(data_dir) / f"{nonce}.json"


def _sign(payload: str, api_token: str) -> str:
    digest = hmac.new(
        api_token.encode(),
        _PAIRING_CONTEXT + payload.encode(),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def _encode_payload(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return _b64encode(raw)


def _decode_payload(payload: str) -> dict[str, object]:
    try:
        value = json.loads(_b64decode(payload))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairingGrantError("invalid pairing code payload") from exc
    if not isinstance(value, dict):
        raise PairingGrantError("invalid pairing code payload")
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8")
