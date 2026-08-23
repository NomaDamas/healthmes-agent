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

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from errno import EACCES, EAGAIN, EDEADLK
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.calendars.base import CalendarAuthError, CalendarError
from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.store.enums import CalendarSource

if TYPE_CHECKING:  # pragma: no cover — typing only
    from healthmes.config import Settings

__all__ = [
    "CalDavCredentials",
    "GoogleConnectionState",
    "StaleCalendarConnectionOperation",
    "begin_calendar_connection_operation",
    "calendar_account_generation",
    "calendar_account_generations",
    "calendar_connection_operation_path",
    "calendar_connection_generation",
    "calendar_connection_write",
    "calendar_credential_file_lock",
    "caldav_environment_managed",
    "caldav_credentials_path",
    "complete_calendar_connection_operation",
    "delete_caldav_credentials",
    "delete_google_token",
    "google_connected",
    "google_connection_state",
    "invalidate_calendar_connection_operation",
    "load_caldav_credentials",
    "resolve_caldav_credentials",
    "save_caldav_credentials",
    "validate_caldav_connection",
    "write_owner_only_json",
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
GOOGLE_ACCOUNT_GENERATION_KEY = "_healthmes_account_generation"
_CONNECTION_OPERATION_VERSION = 1
_CONNECTION_OPERATION_PHASES = frozenset({"pending", "completed", "superseded"})
if os.name == "nt":  # pragma: no cover - exercised on Windows runners
    import msvcrt
else:
    import fcntl


@dataclass(frozen=True, slots=True)
class CalDavCredentials:
    """One resolved CalDAV credential set (values are secret — never log)."""

    username: str
    app_password: str
    url: str
    source: Literal["env", "file"]
    account_generation: str


class StaleCalendarConnectionOperation(CalendarError):
    """A remote connection result no longer owns the source's write slot."""


def _new_account_generation() -> str:
    return uuid.uuid4().hex


def _deterministic_account_generation(
    source: CalendarSource,
    *parts: str,
) -> str:
    encoded = "\x1f".join(
        ("healthmes-calendar-account-v1", source.value, *parts)
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_account_generation(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) not in {32, 64}:
        return None
    if any(character not in "0123456789abcdef" for character in normalized):
        return None
    return normalized


def _valid_connection_operation_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) != 32:
        return None
    if any(character not in "0123456789abcdef" for character in normalized):
        return None
    return normalized


# --- iCloud CalDAV credentials file ------------------------------------------


def caldav_credentials_path(data_dir: Path) -> Path:
    """Owner-only credentials file written by ``healthmes connect icloud``."""
    return Path(data_dir) / "caldav" / "credentials.json"


def calendar_connection_operation_path(
    data_dir: Path,
    source: CalendarSource,
) -> Path:
    """Durable operation state shared by web and CLI connection processes."""

    return (
        Path(data_dir)
        / "calendar-connections"
        / f"{source.value}.operation.json"
    )


def write_owner_only_json(path: Path, payload: dict) -> None:
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


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {EACCES, EAGAIN, EDEADLK}:
                    raise
                time.sleep(0.05)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def calendar_credential_file_lock(path: Path) -> Iterator[None]:
    """Serialize one credential file across service and CLI processes."""

    secret_path = Path(path)
    lock_path = secret_path.with_name(f".{secret_path.name}.healthmes.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "a+b")
    try:
        _lock_file(handle)
        yield
    finally:
        try:
            _unlock_file(handle)
        finally:
            handle.close()


@contextmanager
def calendar_connection_write(
    session: Session,
    source: CalendarSource,
) -> Iterator[None]:
    """Linearize a credential mutation with Decision finalization.

    The credential file is the live calendar-consent boundary. Holding the
    same process and database write-plane fences used by Decision finalization
    guarantees that either the decision commits first or the connection
    mutation becomes visible first; stale mirror rows cannot be re-authorized
    by a racing finalizer.
    """

    with calendar_write_lock(session, source):
        with activity_write_lock():
            lock_activity_write_plane(session)
            try:
                yield
            finally:
                # This fence owns no database mutation. Ending the transaction
                # releases SQLite/PostgreSQL locks without introducing a commit
                # failure after the credential file changed atomically.
                session.rollback()


def _read_connection_operation(
    path: Path,
    source: CalendarSource,
) -> tuple[str, str] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("version") != _CONNECTION_OPERATION_VERSION
        or payload.get("source") != source.value
    ):
        return None
    operation_id = _valid_connection_operation_id(
        payload.get("operation_id")
    )
    phase = payload.get("phase")
    if operation_id is None or phase not in _CONNECTION_OPERATION_PHASES:
        return None
    return operation_id, phase


def _write_connection_operation(
    path: Path,
    source: CalendarSource,
    operation_id: str,
    phase: Literal["pending", "completed", "superseded"],
) -> None:
    write_owner_only_json(
        path,
        {
            "version": _CONNECTION_OPERATION_VERSION,
            "source": source.value,
            "operation_id": operation_id,
            "phase": phase,
        },
    )


def begin_calendar_connection_operation(
    data_dir: Path,
    source: CalendarSource,
) -> str:
    """Create the durable identity for one remote connect/reconnect attempt."""

    operation_id = uuid.uuid4().hex
    path = calendar_connection_operation_path(data_dir, source)
    with calendar_credential_file_lock(path):
        _write_connection_operation(
            path,
            source,
            operation_id,
            "pending",
        )
    return operation_id


def invalidate_calendar_connection_operation[OperationResult](
    data_dir: Path,
    source: CalendarSource,
    apply: Callable[[], OperationResult],
) -> OperationResult:
    """Supersede in-flight work and apply a local mutation atomically.

    The source operation lock stays held while ``apply`` mutates the
    credential file. A new connect cannot begin between invalidation and a
    disconnect, and an older remote completion cannot race the mutation.
    """

    operation_id = uuid.uuid4().hex
    path = calendar_connection_operation_path(data_dir, source)
    with calendar_credential_file_lock(path):
        _write_connection_operation(
            path,
            source,
            operation_id,
            "superseded",
        )
        return apply()


def complete_calendar_connection_operation[OperationResult](
    data_dir: Path,
    source: CalendarSource,
    operation_id: str,
    apply: Callable[[], OperationResult],
) -> OperationResult:
    """Apply a credential result only while its durable operation is current.

    The operation-file lock remains held while ``apply`` atomically replaces
    the separate credential file. A newer web or CLI connect/disconnect can
    therefore either win before this check or supersede this completed write,
    but a stale remote result can never become the active credential.
    """

    expected = _valid_connection_operation_id(operation_id)
    path = calendar_connection_operation_path(data_dir, source)
    with calendar_credential_file_lock(path):
        current = _read_connection_operation(path, source)
        if expected is None or current != (expected, "pending"):
            raise StaleCalendarConnectionOperation(
                f"stale {source.value} calendar connection completion"
            )
        result = apply()
        _write_connection_operation(
            path,
            source,
            expected,
            "completed",
        )
        return result


def save_caldav_credentials(
    data_dir: Path,
    *,
    username: str,
    app_password: str,
    url: str,
    account_generation: str | None = None,
) -> Path:
    """Persist an iCloud/CalDAV credential set owner-only; returns the path."""
    if not username.strip():
        raise CalendarError("caldav username must be non-empty")
    if not app_password:
        raise CalendarError("caldav app password must be non-empty")
    generation = (
        _valid_account_generation(account_generation)
        if account_generation is not None
        else _new_account_generation()
    )
    if generation is None:
        raise CalendarError("caldav account generation is invalid")
    path = caldav_credentials_path(data_dir)
    with calendar_credential_file_lock(path):
        write_owner_only_json(
            path,
            {
                "username": username,
                "app_password": app_password,
                "url": url,
                "account_generation": generation,
            },
        )
    return path


def load_caldav_credentials(data_dir: Path) -> CalDavCredentials | None:
    """Read the stored CalDAV credentials; ``None`` when absent or unusable.

    A corrupt/incomplete file degrades to "not connected" (with a warning that
    names the path, never the contents) instead of failing the caller.
    """
    path = caldav_credentials_path(data_dir)
    with calendar_credential_file_lock(path):
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
    url = str(data.get("url") or "").strip()
    generation = _valid_account_generation(data.get("account_generation"))
    if generation is None:
        generation = _deterministic_account_generation(
            CalendarSource.CALDAV,
            username,
            app_password,
            url,
        )
    return CalDavCredentials(
        username=username,
        app_password=app_password,
        url=url,
        source="file",
        account_generation=generation,
    )


def delete_caldav_credentials(data_dir: Path) -> bool:
    """Remove the stored CalDAV credentials; True when a file was deleted."""
    path = caldav_credentials_path(data_dir)
    with calendar_credential_file_lock(path):
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
    if caldav_environment_managed(settings):
        return CalDavCredentials(
            username=env_username,
            app_password=env_password,
            url=settings.caldav_url,
            source="env",
            account_generation=_deterministic_account_generation(
                CalendarSource.CALDAV,
                env_username,
                env_password,
                settings.caldav_url,
            ),
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
            account_generation=(
                stored.account_generation
                if _valid_account_generation(stored.account_generation)
                is not None
                else _deterministic_account_generation(
                    CalendarSource.CALDAV,
                    stored.username,
                    stored.app_password,
                    settings.caldav_url,
                )
            ),
        )
    return stored


def caldav_environment_managed(settings: "Settings") -> bool:
    """Whether CalDAV is an operator-managed static environment connection."""

    return bool(
        settings.caldav_username.strip()
        and settings.caldav_app_password.get_secret_value().strip()
    )


def calendar_account_generation(
    settings: "Settings",
    source: CalendarSource,
) -> str | None:
    """Return the stable, secret-safe generation of the connected account."""

    if source is CalendarSource.GOOGLE:
        return _google_connection_snapshot(settings.data_dir)[2]
    resolved = resolve_caldav_credentials(settings)
    return resolved.account_generation if resolved is not None else None


def calendar_account_generations(
    settings: "Settings",
) -> dict[CalendarSource, str]:
    """Return only currently connected Calendar account generations.

    Retained mirror rows are not proof that an account is still connected.
    Callers use an empty mapping as a fail-closed visibility boundary after
    disconnect, credential corruption, or an account switch.
    """

    generations: dict[CalendarSource, str] = {}
    for source in CalendarSource:
        try:
            generation = calendar_account_generation(settings, source)
        except Exception:
            logger.warning(
                "calendar account generation for %s is unavailable",
                source.value,
                exc_info=True,
            )
            continue
        if generation is not None:
            generations[source] = generation
    return generations


def calendar_connection_generation(
    settings: "Settings",
    source: CalendarSource,
) -> str | None:
    """Return a secret-safe fingerprint of the current credential material."""

    if source is CalendarSource.GOOGLE:
        _state, generation, _account_generation = _google_connection_snapshot(
            settings.data_dir
        )
        return generation

    resolved = resolve_caldav_credentials(settings)
    if resolved is None:
        return None
    encoded = "\x1f".join(
        (
            resolved.source,
            resolved.username,
            resolved.app_password,
            resolved.url,
            resolved.account_generation,
        )
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    return _google_connection_snapshot(data_dir)[0]


def _google_connection_snapshot(
    data_dir: Path,
) -> tuple[GoogleConnectionState, str | None, str | None]:
    from healthmes.calendars.google import google_token_path

    path = google_token_path(data_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "not_connected", None, None
    except OSError:
        return "invalid", None, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", hashlib.sha256(raw).hexdigest(), None
    if not isinstance(data, dict):
        return "invalid", hashlib.sha256(raw).hexdigest(), None
    if all(str(data.get(key) or "").strip() for key in _GOOGLE_REFRESH_KEYS):
        account_generation = _valid_account_generation(
            data.get(GOOGLE_ACCOUNT_GENERATION_KEY)
        )
        if account_generation is None:
            account_generation = _deterministic_account_generation(
                CalendarSource.GOOGLE,
                str(data["refresh_token"]),
                str(data["client_id"]),
            )
        return (
            "connected",
            hashlib.sha256(raw).hexdigest(),
            account_generation,
        )
    return "invalid", hashlib.sha256(raw).hexdigest(), None


def google_connected(data_dir: Path) -> bool:
    """True when a usable Google authorization is stored under ``data_dir``."""
    return google_connection_state(data_dir) == "connected"


def delete_google_token(data_dir: Path) -> bool:
    """Remove the stored Google token; True when a file was deleted."""
    from healthmes.calendars.google import google_token_path

    path = google_token_path(data_dir)
    with calendar_credential_file_lock(path):
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True
