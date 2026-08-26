"""Short-lived companion-app pairing grants and terminal QR rendering.

Pairing QR payloads contain only an approved HealthMes origin and a random
one-time code::

    healthmes://pair?url=<base>&code=<short-lived-code>

The long-lived bearer token remains server-side until the companion exchanges
the code through ``POST /v1/setup/pairing/exchange``. Grants expire after five
minutes and are claimed with an atomic active-to-consumed rename.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from healthmes.config import Settings, is_loopback_host
from healthmes.durable_files import ensure_durable_directory, open_directory_anchored

PAIRING_GRANT_TTL = timedelta(minutes=5)
PAIRING_EXCHANGE_PATH = "/v1/setup/pairing/exchange"
_PAIRING_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_GRANT_SCHEMA = "healthmes.pairing-grant.v1"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - directory fsync is POSIX-only
        return
    with open_directory_anchored(path) as (_canonical, descriptor):
        os.fsync(descriptor)


class PairingGrantError(RuntimeError):
    """Base class for one-time grant failures."""


class PairingGrantUnknown(PairingGrantError):
    pass


class PairingGrantConsumed(PairingGrantError):
    pass


class PairingGrantExpired(PairingGrantError):
    pass


class PairingGrantCorrupt(PairingGrantError):
    pass


class PairingGrantStore:
    """Small cross-process grant store under the server data directory.

    Active grants contain only a SHA-256-derived filename, the approved base
    URL, and an expiry. The long-lived API token remains in server settings.
    An atomic rename from ``active`` to ``consumed`` or ``expired`` is the
    one-time claim boundary across workers and setup-script processes.
    """

    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir) / "pairing-grants"
        self._active = self._root / "active"
        self._consumed = self._root / "consumed"
        self._expired = self._root / "expired"

    def issue(
        self,
        base_url: str,
        *,
        now: datetime | None = None,
        ttl: timedelta = PAIRING_GRANT_TTL,
    ) -> tuple[str, datetime]:
        if not is_secure_pairing_origin(base_url):
            raise ValueError(
                "pairing requires HTTPS, or loopback HTTP for the same device"
            )
        if ttl <= timedelta(0) or ttl > PAIRING_GRANT_TTL:
            raise ValueError(
                "pairing grant ttl must be positive and no longer than five minutes"
            )
        issued_at = _aware_utc(now or datetime.now(UTC))
        expires_at = issued_at + ttl
        code = secrets.token_urlsafe(32)
        digest = self._digest(code)
        self._ensure_directories()
        self._write_owner_only_json(
            self._active / f"{digest}.json",
            {
                "schema": _GRANT_SCHEMA,
                "base_url": base_url,
                "issued_at": issued_at.timestamp(),
                "expires_at": expires_at.timestamp(),
            },
        )
        return code, expires_at

    def consume(
        self,
        code: str,
        *,
        now: datetime | None = None,
        validate_base_url: Callable[[str], None] | None = None,
    ) -> str:
        normalized = code.strip()
        if _PAIRING_CODE_PATTERN.fullmatch(normalized) is None:
            raise PairingGrantUnknown
        digest = self._digest(normalized)
        active = self._active / f"{digest}.json"
        consumed = self._consumed / f"{digest}.json"
        expired = self._expired / f"{digest}.json"
        self._ensure_directories()
        if consumed.exists():
            raise PairingGrantConsumed
        if expired.exists():
            raise PairingGrantExpired
        try:
            record = self._read_record(active)
        except FileNotFoundError:
            return self._resolve_terminal_state(consumed, expired)

        current = _aware_utc(now or datetime.now(UTC))
        try:
            issued_at = datetime.fromtimestamp(record["issued_at"], tz=UTC)
            expires_at = datetime.fromtimestamp(record["expires_at"], tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise PairingGrantCorrupt from exc
        if current < issued_at:
            raise PairingGrantCorrupt
        if current >= expires_at:
            destination = expired
        else:
            if validate_base_url is not None:
                validate_base_url(record["base_url"])
            destination = consumed
        try:
            os.replace(active, destination)
        except FileNotFoundError:
            return self._resolve_terminal_state(consumed, expired)
        _fsync_directory(active.parent)
        if destination.parent != active.parent:
            _fsync_directory(destination.parent)
        if destination == expired:
            raise PairingGrantExpired
        return record["base_url"]

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode("ascii")).hexdigest()

    @staticmethod
    def _resolve_terminal_state(consumed: Path, expired: Path) -> str:
        if consumed.exists():
            raise PairingGrantConsumed
        if expired.exists():
            raise PairingGrantExpired
        raise PairingGrantUnknown

    def _ensure_directories(self) -> None:
        for directory in (
            self._root,
            self._active,
            self._consumed,
            self._expired,
        ):
            ensure_durable_directory(directory, mode=0o700)
            if os.name != "nt":
                directory.chmod(0o700)

    @staticmethod
    def _write_owner_only_json(path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            else:
                _fsync_directory(path.parent)
            raise

    @staticmethod
    def _read_record(path: Path) -> dict[str, str | float]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise PairingGrantCorrupt
            if os.name != "nt" and metadata.st_mode & 0o077:
                raise PairingGrantCorrupt
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PairingGrantCorrupt from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(payload, dict) or payload.get("schema") != _GRANT_SCHEMA:
            raise PairingGrantCorrupt
        base_url = payload.get("base_url")
        issued_at = payload.get("issued_at")
        expires_at = payload.get("expires_at")
        if (
            not isinstance(base_url, str)
            or not is_secure_pairing_origin(base_url)
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
        ):
            raise PairingGrantCorrupt
        issued_timestamp = float(issued_at)
        expires_timestamp = float(expires_at)
        lifetime = expires_timestamp - issued_timestamp
        if (
            not math.isfinite(issued_timestamp)
            or not math.isfinite(expires_timestamp)
            or lifetime <= 0
            or lifetime > PAIRING_GRANT_TTL.total_seconds()
        ):
            raise PairingGrantCorrupt
        return {
            "base_url": base_url,
            "issued_at": issued_timestamp,
            "expires_at": expires_timestamp,
        }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_secure_pairing_origin(base_url: str) -> bool:
    """True for HTTPS or same-device loopback HTTP origins without secrets."""

    if (
        not base_url
        or base_url != base_url.strip()
        or any(character.isspace() for character in base_url)
        or not base_url.isprintable()
    ):
        return False
    try:
        parsed = urlsplit(base_url)
        parsed.port
    except ValueError:
        return False
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and is_loopback_host(parsed.hostname)


def is_loopback_pairing_origin(base_url: str) -> bool:
    """True only for credential-free HTTP(S) origins on this device."""

    parsed = urlsplit(base_url)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and is_loopback_host(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def build_pairing_url(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str:
    """Issue a one-time grant and return its ``healthmes://pair`` deep link."""

    base_url = settings.public_base_url.rstrip("/")
    if not is_secure_pairing_origin(base_url):
        raise ValueError(
            "pairing requires HTTPS, or loopback HTTP for the same device"
        )
    code, _expires_at = PairingGrantStore(settings.data_dir).issue(
        base_url,
        now=now,
    )
    return (
        f"healthmes://pair?url={quote(base_url, safe='')}"
        f"&code={quote(code, safe='')}"
    )


def render_terminal_qr(payload: str) -> str:
    """Render one pairing deep link as a compact terminal QR block."""

    import io

    import segno

    qr = segno.make(payload, error="m")
    buffer = io.StringIO()
    qr.terminal(out=buffer, compact=True, border=2)
    return buffer.getvalue()
