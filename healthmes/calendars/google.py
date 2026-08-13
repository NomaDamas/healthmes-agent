"""Google Calendar backend (docs/PLAN.md section 6).

- ``syncToken`` incremental sync with automatic full resync on HTTP 410 GONE
  (the flow documented in the Calendar API sync guide).
- OAuth *installed app* helpers; the token lives under
  ``Settings.data_dir/google/`` (pattern follows
  ``vendor/hermes-agent/skills/productivity/google-workspace/scripts/google_api.py``).
- Agent ownership tag: private extended property ``healthmes=1`` plus
  ``healthmes_task_id`` (see :mod:`healthmes.calendars.base`).

Credentials are runtime-only: google client libraries are imported lazily
inside functions, the API ``service`` is injected into the backend, and no
token file is touched at import time. Tests drive the backend with a fake
service object.

Known limitation (documented tradeoff): the initial/full sync lists a
``[now - lookback_days, now + horizon_days]`` window with ``singleEvents=True``
(unbounded recurring expansion would paginate forever without a window). After
a 410 full resync, ids previously known but absent from the fresh window are
reported as deletions — events that merely slid out of the window are pruned
from the mirror the same way, which keeps the mirror bounded to the active
scheduling horizon.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from healthmes.calendars.base import (
    AGENT_TAG_VALUE,
    GOOGLE_AGENT_TAG_KEY,
    GOOGLE_AGENT_TASK_ID_KEY,
    GOOGLE_EVENT_KIND_KEY,
    GOOGLE_EVENT_SOURCE_KEY,
    GOOGLE_EVENT_UPSERT_KEY,
    CalendarAuthError,
    CalendarConflictError,
    CalendarError,
    ConfirmedExternalTimeChange,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    SyncState,
    calendar_identity_external_id,
    ensure_utc,
    parse_calendar_identity,
    parse_event_kind,
    parse_task_id,
)
from healthmes.calendars.creds import (
    GOOGLE_ACCOUNT_GENERATION_KEY,
    calendar_credential_file_lock,
    write_owner_only_json,
)
from healthmes.calendars.state import (
    SyncCoverageKind,
    sync_state_coverage,
    with_sync_state_coverage,
)
from healthmes.store.enums import CalendarSource

__all__ = [
    "GOOGLE_SCOPES",
    "GoogleCalendarBackend",
    "ensure_credentials",
    "google_client_secret_path",
    "google_token_path",
    "load_credentials",
    "run_installed_app_flow",
    "save_credentials",
]

logger = logging.getLogger(__name__)

#: Minimal scope: full read/write on events only (no calendar administration).
GOOGLE_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar.events",)

_MAX_PAGES = 100  # hard stop against pagination loops


# --- OAuth installed-app helpers (token under Settings.data_dir) -------------


def google_token_path(data_dir: Path) -> Path:
    """Stored-authorization (token) file location under the data dir."""
    return Path(data_dir) / "google" / "calendar_token.json"


def google_client_secret_path(data_dir: Path) -> Path:
    """OAuth client secret location; the user downloads this from Google Cloud."""
    return Path(data_dir) / "google" / "client_secret.json"


def save_credentials(
    credentials: Any,
    token_file: Path,
    *,
    account_generation: str | None = None,
) -> None:
    """Persist authorized-user credentials as JSON, owner-readable only."""
    token_file = Path(token_file)
    payload = json.loads(credentials.to_json())
    if not isinstance(payload, dict):
        raise ValueError("Google credentials must serialize to an object")
    payload.setdefault("type", "authorized_user")
    payload[GOOGLE_ACCOUNT_GENERATION_KEY] = (
        account_generation or uuid.uuid4().hex
    )
    with calendar_credential_file_lock(token_file):
        write_owner_only_json(token_file, payload)


def _token_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _save_refreshed_credentials(
    credentials: Any,
    token_file: Path,
    *,
    expected_digest: str,
) -> bool:
    """Persist a refresh only when the authorization file is unchanged."""

    with calendar_credential_file_lock(token_file):
        try:
            current = token_file.read_bytes()
        except FileNotFoundError:
            return False
        if _token_digest(current) != expected_digest:
            return False
        payload = json.loads(credentials.to_json())
        if not isinstance(payload, dict):
            raise ValueError("Google credentials must serialize to an object")
        payload.setdefault("type", "authorized_user")
        try:
            current_payload = json.loads(current.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(current_payload, dict):
            return False
        account_generation = current_payload.get(
            GOOGLE_ACCOUNT_GENERATION_KEY
        )
        if not isinstance(account_generation, str) or not account_generation:
            refresh_token = str(
                current_payload.get("refresh_token") or ""
            )
            client_id = str(current_payload.get("client_id") or "")
            account_generation = hashlib.sha256(
                "\x1f".join(
                    (
                        "healthmes-calendar-account-v1",
                        CalendarSource.GOOGLE.value,
                        refresh_token,
                        client_id,
                    )
                ).encode("utf-8")
            ).hexdigest()
        payload[GOOGLE_ACCOUNT_GENERATION_KEY] = account_generation
        write_owner_only_json(token_file, payload)
        return True


def load_credentials(token_file: Path, scopes: Sequence[str] = GOOGLE_SCOPES) -> Any | None:
    """Load stored credentials, refreshing (and re-persisting) if expired.

    Returns ``None`` when no usable authorization exists (missing/unreadable
    file, or invalid without a refresh token). Never interactive.
    """
    token_file = Path(token_file)
    from google.oauth2.credentials import Credentials

    with calendar_credential_file_lock(token_file):
        try:
            raw = token_file.read_bytes()
        except FileNotFoundError:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            authorized_user_info = dict(payload)
            authorized_user_info.pop(
                GOOGLE_ACCOUNT_GENERATION_KEY,
                None,
            )
            credentials = Credentials.from_authorized_user_info(
                authorized_user_info,
                list(scopes),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            logger.warning(
                "unreadable google token file %s; re-auth required",
                token_file,
            )
            return None

    if credentials.expired and credentials.refresh_token:
        from google.auth.transport.requests import Request

        expected_digest = _token_digest(raw)
        # Refresh is remote I/O. Disconnect and reconnect must not wait for it;
        # the compare-and-swap below rejects a result from stale credentials.
        credentials.refresh(Request())
        if not _save_refreshed_credentials(
            credentials,
            token_file,
            expected_digest=expected_digest,
        ):
            logger.info(
                "google token changed during refresh; discarding stale "
                "authorization"
            )
            return None

    return credentials if credentials.valid else None


def run_installed_app_flow(
    client_secret_file: Path,
    token_file: Path,
    scopes: Sequence[str] = GOOGLE_SCOPES,
    *,
    port: int = 0,
    persist: bool = True,
) -> Any:
    """Run the interactive installed-app OAuth flow and persist the token.

    Opens a local browser and a loopback listener; only ever call this from
    interactive tooling (never from the service loop).
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), list(scopes))
    credentials = flow.run_local_server(port=port)
    if persist:
        save_credentials(credentials, token_file)
    return credentials


def ensure_credentials(
    data_dir: Path,
    *,
    scopes: Sequence[str] = GOOGLE_SCOPES,
    interactive: bool = False,
    port: int = 0,
) -> Any:
    """Return valid credentials from ``data_dir``, optionally bootstrapping.

    Non-interactive mode (the service default) raises
    :class:`CalendarAuthError` instead of blocking on a browser flow.
    """
    token_file = google_token_path(data_dir)
    credentials = load_credentials(token_file, scopes)
    if credentials is not None:
        return credentials
    if not interactive:
        raise CalendarAuthError(
            f"no valid Google authorization at {token_file}; "
            "run the interactive setup (scripts/bootstrap) first"
        )
    client_secret_file = google_client_secret_path(data_dir)
    if not client_secret_file.exists():
        raise CalendarAuthError(
            f"missing OAuth client secret at {client_secret_file}; "
            "download it from Google Cloud Console (Desktop app credentials)"
        )
    return run_installed_app_flow(client_secret_file, token_file, scopes, port=port)


def build_calendar_service(credentials: Any) -> Any:
    """Build the ``calendar v3`` API service resource from credentials."""
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


# --- backend -----------------------------------------------------------------


def _http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status of a client exception.

    Reads ``status_code`` (googleapiclient>=2 ``HttpError`` property) then
    ``resp.status`` (httplib2 response) so tests can raise plain stubs.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return status if isinstance(status, int) else None


def _rfc3339(value: datetime) -> str:
    """UTC RFC3339 timestamp for API request bodies/parameters."""
    return ensure_utc(value).isoformat()


class GoogleCalendarBackend:
    """Google Calendar API backend over an injected ``calendar v3`` service.

    ``sync_state`` layout::

        {"sync_token": "<nextSyncToken>", "known_ids": {"<event id>": "<etag>"}}

    ``known_ids`` mirrors the ids delivered so far so that a 410-triggered
    full resync can synthesize deletion notices for events that disappeared
    while the token was invalid.
    """

    source = CalendarSource.GOOGLE

    def __init__(
        self,
        service: Any,
        calendar_id: str = "primary",
        *,
        lookback_days: int = 30,
        horizon_days: int = 365,
        page_size: int = 250,
    ) -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._lookback_days = lookback_days
        self._horizon_days = horizon_days
        self._page_size = page_size

    @classmethod
    def from_data_dir(
        cls,
        data_dir: Path,
        calendar_id: str = "primary",
        *,
        interactive: bool = False,
        **kwargs: Any,
    ) -> "GoogleCalendarBackend":
        """Build a live backend from the token stored under ``data_dir``."""
        credentials = ensure_credentials(data_dir, interactive=interactive)
        return cls(build_calendar_service(credentials), calendar_id, **kwargs)

    # -- change feed -----------------------------------------------------

    def list_changes(
        self, sync_state: SyncState | None
    ) -> tuple[list[ExternalEvent], SyncState]:
        previous = dict(sync_state or {})
        sync_token = previous.get("sync_token")
        known_ids: dict[str, str] = dict(previous.get("known_ids") or {})
        coverage_kind, _, _ = sync_state_coverage(previous)

        if (
            sync_token
            and coverage_kind is SyncCoverageKind.BOUNDED_WINDOW
        ):
            try:
                return self._incremental_sync(
                    str(sync_token),
                    known_ids,
                    previous,
                )
            except Exception as exc:  # noqa: BLE001 - status-based dispatch
                if _http_status(exc) != 410:
                    raise
                logger.info(
                    "google sync token expired (410) for calendar %s; full resync",
                    self._calendar_id,
                )
        return self._full_sync(known_ids)

    def _incremental_sync(
        self,
        sync_token: str,
        known_ids: dict[str, str],
        previous_state: SyncState,
    ) -> tuple[list[ExternalEvent], SyncState]:
        items, next_token = self._list_pages({"syncToken": sync_token})
        events = [self._parse_api_event(item) for item in items]
        new_known = dict(known_ids)
        for event in events:
            if event.deleted:
                new_known.pop(event.external_id, None)
            else:
                new_known[event.external_id] = event.etag or ""
        state: SyncState = {
            "sync_token": next_token,
            "known_ids": new_known,
        }
        coverage_kind, coverage_start, coverage_end = sync_state_coverage(
            previous_state
        )
        if coverage_kind is SyncCoverageKind.BOUNDED_WINDOW:
            return events, with_sync_state_coverage(
                state,
                kind=coverage_kind,
                start=coverage_start,
                end=coverage_end,
            )
        return events, state

    def _full_sync(self, known_ids: dict[str, str]) -> tuple[list[ExternalEvent], SyncState]:
        now = datetime.now(UTC)
        coverage_start = now - timedelta(days=self._lookback_days)
        coverage_end = now + timedelta(days=self._horizon_days)
        params = {
            "singleEvents": True,
            "timeMin": _rfc3339(coverage_start),
            "timeMax": _rfc3339(coverage_end),
        }
        items, next_token = self._list_pages(params)
        events = [self._parse_api_event(item) for item in items]
        events = [event for event in events if not event.deleted]
        current_ids = {event.external_id: event.etag or "" for event in events}
        deletions = [
            ExternalEvent(external_id=event_id, deleted=True)
            for event_id in known_ids
            if event_id not in current_ids
        ]
        return events + deletions, with_sync_state_coverage(
            {"sync_token": next_token, "known_ids": current_ids},
            kind=SyncCoverageKind.BOUNDED_WINDOW,
            start=coverage_start,
            end=coverage_end,
        )

    def _list_pages(self, base_params: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """Drain ``events.list`` pagination; return (items, nextSyncToken)."""
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "calendarId": self._calendar_id,
                "maxResults": self._page_size,
                **base_params,
            }
            if page_token:
                params["pageToken"] = page_token
            response = self._events().list(**params).execute()
            items.extend(response.get("items") or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return items, response.get("nextSyncToken")
        raise CalendarError(
            f"google events.list pagination exceeded {_MAX_PAGES} pages "
            f"for calendar {self._calendar_id!r}"
        )

    # -- agent writes ------------------------------------------------------

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        private = {GOOGLE_AGENT_TAG_KEY: AGENT_TAG_VALUE}
        if draft.agent_task_id is not None:
            private[GOOGLE_AGENT_TASK_ID_KEY] = str(draft.agent_task_id)
        if draft.identity is not None:
            private[GOOGLE_EVENT_KIND_KEY] = draft.identity.kind.value
            private[GOOGLE_EVENT_SOURCE_KEY] = draft.identity.source
            private[GOOGLE_EVENT_UPSERT_KEY] = draft.identity.source_key
        body: dict[str, Any] = {
            "summary": draft.summary,
            "start": {"dateTime": _rfc3339(draft.start_at)},
            "end": {"dateTime": _rfc3339(draft.end_at)},
            "extendedProperties": {"private": private},
        }
        if draft.identity is not None:
            body["id"] = calendar_identity_external_id(self.source, draft.identity)
        if draft.description:
            body["description"] = draft.description
        try:
            created = (
                self._events()
                .insert(calendarId=self._calendar_id, body=body)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - status-based dispatch
            if _http_status(exc) != 409 or draft.identity is None:
                raise
            raise CalendarConflictError(
                "google identity key already exists"
            ) from exc
        return self._parse_api_event(created)

    def update_event(
        self,
        external_id: str,
        *,
        summary: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        description: str | None = None,
        expected_etag: str | None = None,
    ) -> ExternalEvent:
        current = self._get_owned_event(external_id)
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if start_at is not None:
            body["start"] = {"dateTime": _rfc3339(start_at)}
        if end_at is not None:
            body["end"] = {"dateTime": _rfc3339(end_at)}
        if description is not None:
            body["description"] = description
        if not body:
            return current
        # The caller's mirror ETag protects the entire sync-to-write interval;
        # the fresh ETag is only the fallback for callers without a snapshot.
        request = self._events().patch(
            calendarId=self._calendar_id, eventId=external_id, body=body
        )
        self._set_if_match(request, expected_etag or current.etag)
        try:
            patched = request.execute()
        except Exception as exc:  # noqa: BLE001 - status-based dispatch
            self._raise_for_write_status(exc, external_id)
            raise  # unreachable; keeps type-checkers happy about ``patched``
        return self._parse_api_event(patched)

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
    ) -> None:
        current = self._get_owned_event(external_id)
        if expected_kind is not None and current.healthmes_kind is not expected_kind:
            raise OwnershipError(
                f"google event {external_id!r} is not {expected_kind.value}"
            )
        request = self._events().delete(
            calendarId=self._calendar_id, eventId=external_id
        )
        self._set_if_match(request, expected_etag or current.etag)
        try:
            request.execute()
        except Exception as exc:  # noqa: BLE001 - status-based dispatch
            self._raise_for_write_status(exc, external_id)
            raise  # unreachable; _raise_for_write_status always raises

    def read_event(self, external_id: str) -> ExternalEvent:
        return self._get_event(external_id)

    def apply_confirmed_external_time_change(
        self, change: ConfirmedExternalTimeChange
    ) -> ExternalEvent:
        body = {"end": {"dateTime": _rfc3339(change.proposed_end_at)}}
        request = self._events().patch(
            calendarId=self._calendar_id,
            eventId=change.external_event_id,
            body=body,
        )
        self._set_if_match(request, change.expected_etag)
        try:
            patched = request.execute()
        except Exception as exc:  # noqa: BLE001 - status-based dispatch
            self._raise_for_write_status(exc, change.external_event_id)
            raise
        event = self._parse_api_event(patched)
        if event.external_id != change.external_event_id:
            raise CalendarError(
                f"google patch returned event {event.external_id!r}, "
                f"expected {change.external_event_id!r}"
            )
        if event.start_at != change.proposed_start_at or event.end_at != change.proposed_end_at:
            raise CalendarError(
                f"google patch returned unexpected time range for "
                f"{change.external_event_id!r}"
            )
        return event

    @staticmethod
    def _set_if_match(request: Any, etag: str | None) -> None:
        """Attach an ``If-Match`` precondition to a googleapiclient request.

        ``HttpRequest`` exposes a mutable ``headers`` dict; sending the etag
        makes patch/delete conditional so a concurrent remote edit is rejected
        (HTTP 412) rather than overwritten.
        """
        if etag:
            request.headers["If-Match"] = etag

    @staticmethod
    def _raise_for_write_status(exc: BaseException, external_id: str) -> None:
        """Map a patch/delete failure to the right domain error (always raises)."""
        status = _http_status(exc)
        if status == 412:
            raise CalendarConflictError(
                f"google event {external_id!r} changed on the server since it was "
                "read (If-Match precondition failed); re-sync the mirror and retry"
            ) from exc
        if status in (404, 410):
            raise EventNotFoundError(f"google event {external_id!r} not found") from exc
        raise exc

    def _get_owned_event(self, external_id: str) -> ExternalEvent:
        """Fetch + parse an event, enforcing the agent-ownership tag."""
        event = self._get_event(external_id)
        if event.deleted:
            raise EventNotFoundError(f"google event {external_id!r} is cancelled")
        if not event.is_agent_created:
            raise OwnershipError(
                f"google event {external_id!r} is not agent-created "
                "(missing healthmes=1 extended property); the external calendar owns it"
            )
        return event

    def _get_event(self, external_id: str) -> ExternalEvent:
        try:
            item = (
                self._events()
                .get(calendarId=self._calendar_id, eventId=external_id)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - status-based dispatch
            if _http_status(exc) in (404, 410):
                raise EventNotFoundError(f"google event {external_id!r} not found") from exc
            raise
        return self._parse_api_event(item)

    # -- parsing -----------------------------------------------------------

    def _parse_api_event(self, item: dict[str, Any]) -> ExternalEvent:
        external_id = item.get("id")
        if not external_id:
            raise CalendarError(f"google event without id: {item!r}")
        private = (item.get("extendedProperties") or {}).get("private") or {}
        is_agent = str(private.get(GOOGLE_AGENT_TAG_KEY, "")) == AGENT_TAG_VALUE
        agent_task_id = parse_task_id(private.get(GOOGLE_AGENT_TASK_ID_KEY))
        status = item.get("status")
        start = item.get("start")
        end = item.get("end")
        deleted = status == "cancelled"
        return ExternalEvent(
            external_id=external_id,
            summary=item.get("summary") or None,
            description=item.get("description") or None,
            start_at=_parse_api_time(start),
            end_at=_parse_api_time(end),
            is_agent_created=is_agent,
            agent_task_id=agent_task_id,
            identity=parse_calendar_identity(
                private.get(GOOGLE_EVENT_KIND_KEY),
                private.get(GOOGLE_EVENT_SOURCE_KEY),
                private.get(GOOGLE_EVENT_UPSERT_KEY),
            ),
            healthmes_kind=parse_event_kind(private.get(GOOGLE_EVENT_KIND_KEY)),
            etag=item.get("etag"),
            deleted=deleted,
            organizer_self=_is_self(item.get("organizer")),
            has_attendees=bool(item.get("attendees") or []),
            is_recurring=bool(item.get("recurrence") or item.get("recurringEventId")),
            event_type=item.get("eventType") or "default",
            is_all_day=_is_all_day_time(start) or _is_all_day_time(end),
            is_locked=bool(item.get("locked")),
            status=status,
        )

    def _events(self) -> Any:
        return self._service.events()


def _parse_api_time(value: dict[str, Any] | None) -> datetime | None:
    """Parse a Google ``start``/``end`` object (dateTime or all-day date).

    All-day dates map to midnight UTC (the mirror stores instants only);
    Google's all-day ``end.date`` is exclusive, which this mapping preserves.
    """
    if not value:
        return None
    if value.get("dateTime"):
        return ensure_utc(datetime.fromisoformat(value["dateTime"]))
    if value.get("date"):
        day = date.fromisoformat(value["date"])
        return datetime.combine(day, time.min, tzinfo=UTC)
    return None


def _is_all_day_time(value: dict[str, Any] | None) -> bool:
    return bool(value and value.get("date") and not value.get("dateTime"))


def _is_self(value: object) -> bool:
    return isinstance(value, dict) and bool(value.get("self"))
