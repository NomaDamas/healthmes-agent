"""Runtime calendar-connection credentials (the ``healthmes connect`` layer).

docs/PLAN.md §6 keeps calendar credentials runtime-only. This module is the
single place that decides whether a calendar is *connected* and where the
secret material for a runtime connection lives:

- **Google** — the OAuth token minted by the installed-app flow at
  ``{data_dir}/google/calendar_token.json`` (healthmes/calendars/google.py).
  "Connected" is judged offline: the file parses as an authorized-user
  document carrying the refresh material Google needs. No google imports, no
  network — the sync job still refreshes (and may fail) at runtime as before.
- **iCloud CalDAV** — ``{data_dir}/caldav/credentials.json`` written by
  ``healthmes connect icloud`` (owner-only, mode 600). The env settings
  (``HEALTHMES_CALDAV_USERNAME`` + ``HEALTHMES_CALDAV_APP_PASSWORD``) keep
  working and OVERRIDE the file when both are set —
  :func:`resolve_caldav_credentials` is the one resolution point the backend
  builder consumes.

Security rules enforced here: credential files are created owner-only from
the first byte (``os.open`` with mode 0600, then an atomic replace), nothing
in this module ever logs or returns a secret except
:func:`resolve_caldav_credentials`/:func:`load_caldav_credentials` (whose
callers are the backend builder and the CLI), and error messages scrub the
app password defensively.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from healthmes.calendars.base import CalendarAuthError, CalendarError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from healthmes.config import Settings

__all__ = [
    "CalDavCredentials",
    "GoogleConnectionState",
    "caldav_credentials_path",
    "delete_caldav_credentials",
    "delete_google_token",
    "google_connected",
    "google_connection_state",
    "load_caldav_credentials",
    "resolve_caldav_credentials",
    "save_caldav_credentials",
    "validate_caldav_connection",
]

logger = logging.getLogger(__name__)

#: Offline judgement of the stored Google authorization (no network):
#: ``connected`` (token file parses with refresh material), ``invalid``
#: (file present but unusable — re-run ``healthmes connect google``) or
#: ``not_connected`` (no token file).
GoogleConnectionState = Literal["connected", "invalid", "not_connected"]

# Keys google.oauth2.credentials.Credentials.from_authorized_user_info
# requires to refresh non-interactively; a token file missing any of them
# cannot survive expiry and counts as broken.
_GOOGLE_REFRESH_KEYS = ("refresh_token", "client_id", "client_secret")


@dataclass(frozen=True, slots=True)
class CalDavCredentials:
    """One resolved CalDAV credential set (values are secret — never log)."""

    username: str
    app_password: str
    url: str
    source: Literal["env", "file"]


# --- iCloud CalDAV credentials file ------------------------------------------


def caldav_credentials_path(data_dir: Path) -> Path:
    """Owner-only credentials file written by ``healthmes connect icloud``."""
    return Path(data_dir) / "caldav" / "credentials.json"


def _write_owner_only_json(path: Path, payload: dict) -> None:
    """Write JSON created with mode 0600 from the first byte, then swap atomically.

    ``os.open`` with the restrictive mode means there is never a window where
    the secret is world-readable; the unique temp name + ``os.replace``
    mirrors healthmes/calendars/state.py so a crash never leaves a torn file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, path)
    path.chmod(0o600)  # replace preserves the temp's 0600; re-assert anyway


def save_caldav_credentials(
    data_dir: Path, *, username: str, app_password: str, url: str
) -> Path:
    """Persist an iCloud/CalDAV credential set owner-only; returns the path."""
    if not username.strip():
        raise CalendarError("caldav username must be non-empty")
    if not app_password:
        raise CalendarError("caldav app password must be non-empty")
    path = caldav_credentials_path(data_dir)
    _write_owner_only_json(
        path, {"username": username, "app_password": app_password, "url": url}
    )
    return path


def load_caldav_credentials(data_dir: Path) -> CalDavCredentials | None:
    """Read the stored CalDAV credentials; ``None`` when absent or unusable.

    A corrupt/incomplete file degrades to "not connected" (with a warning that
    names the path, never the contents) instead of failing the caller.
    """
    path = caldav_credentials_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("unreadable caldav credentials file %s", path)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "corrupt caldav credentials file %s; re-run `healthmes connect icloud`", path
        )
        return None
    if not isinstance(data, dict):
        logger.warning("malformed caldav credentials file %s", path)
        return None
    username = str(data.get("username") or "").strip()
    app_password = str(data.get("app_password") or "")
    if not username or not app_password:
        logger.warning("incomplete caldav credentials file %s", path)
        return None
    return CalDavCredentials(
        username=username,
        app_password=app_password,
        url=str(data.get("url") or "").strip(),
        source="file",
    )


def delete_caldav_credentials(data_dir: Path) -> bool:
    """Remove the stored CalDAV credentials; True when a file was deleted."""
    path = caldav_credentials_path(data_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def resolve_caldav_credentials(settings: "Settings") -> CalDavCredentials | None:
    """The single CalDAV credential resolution point (env first, then file).

    Env wins when BOTH ``caldav_username`` and ``caldav_app_password`` are set
    (the pre-existing configuration path, unchanged); otherwise the creds file
    written by ``healthmes connect icloud`` is used. ``None`` = not connected.
    """
    env_username = settings.caldav_username.strip()
    env_password = settings.caldav_app_password.get_secret_value().strip()
    if env_username and env_password:
        return CalDavCredentials(
            username=env_username,
            app_password=env_password,
            url=settings.caldav_url,
            source="env",
        )
    stored = load_caldav_credentials(settings.data_dir)
    if stored is None:
        return None
    if not stored.url:
        stored = CalDavCredentials(
            username=stored.username,
            app_password=stored.app_password,
            url=settings.caldav_url,
            source="file",
        )
    return stored


def validate_caldav_connection(*, username: str, app_password: str, url: str) -> str:
    """Open a real CalDAV session and discover the principal's calendars.

    Returns a short human description of what was found (calendar names only —
    never credentials). Raises :class:`CalendarAuthError` when the server
    rejects the login or discovery fails, with the app password scrubbed from
    the message defensively. Mirrors the discovery
    :meth:`CalDavCalendarBackend.connect` performs, so a passing validation
    means the sync backend will connect the same way.
    """
    import caldav

    try:
        client = caldav.DAVClient(url=url, username=username, password=app_password)
        principal = client.principal()
        calendars = principal.calendars()
    except Exception as exc:  # noqa: BLE001 - library raises broad errors
        detail = str(exc).replace(app_password, "***") or type(exc).__name__
        raise CalendarAuthError(
            f"CalDAV login/discovery failed for {username} at {url}: {detail}"
        ) from exc
    if not calendars:
        raise CalendarError(
            f"CalDAV login succeeded but no calendars are visible at {url}"
        )
    names = [str(getattr(cal, "name", None) or "?") for cal in calendars]
    shown = ", ".join(names[:3]) + (" …" if len(names) > 3 else "")
    return f"{len(calendars)} calendar(s): {shown}"


# --- Google token (offline connection judgement) ------------------------------


def google_connection_state(data_dir: Path) -> GoogleConnectionState:
    """Offline judgement of the stored Google authorization (no network).

    ``connected`` when the token file parses as an authorized-user JSON with
    the refresh material google-auth needs; ``invalid`` when a file exists but
    is unusable; ``not_connected`` when there is no token file. Deliberately
    import-free of the google libraries so status checks stay instant.
    """
    from healthmes.calendars.google import google_token_path

    path = google_token_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "not_connected"
    except OSError:
        return "invalid"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "invalid"
    if not isinstance(data, dict):
        return "invalid"
    if all(str(data.get(key) or "").strip() for key in _GOOGLE_REFRESH_KEYS):
        return "connected"
    return "invalid"


def google_connected(data_dir: Path) -> bool:
    """True when a usable Google authorization is stored under ``data_dir``."""
    return google_connection_state(data_dir) == "connected"


def delete_google_token(data_dir: Path) -> bool:
    """Remove the stored Google token; True when a file was deleted."""
    from healthmes.calendars.google import google_token_path

    try:
        google_token_path(data_dir).unlink()
    except FileNotFoundError:
        return False
    return True
