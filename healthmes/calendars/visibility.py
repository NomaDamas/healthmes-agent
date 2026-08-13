"""Fail-closed visibility for Calendar mirror consumers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars import creds
from healthmes.calendars.state import FileSyncHealthStore, SyncHealthStore
from healthmes.config import Settings
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror

_MAX_STABLE_READ_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class CalendarVisibility:
    """Account generations whose mirror has completed at least one sync."""

    account_generations: Mapping[CalendarSource, str]
    limitations: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.account_generations)

    @property
    def generation_fingerprint(self) -> str:
        """Return a stable, privacy-safe identity for this visibility set."""

        payload = "\n".join(
            f"{source.value}:{generation}"
            for source, generation in sorted(
                self.account_generations.items(),
                key=lambda item: item[0].value,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def predicate(self) -> sa.ColumnElement[bool]:
        """Return a SQL predicate that exposes only visible mirror rows."""

        filters = tuple(
            sa.and_(
                CalendarEventMirror.calendar_source == source,
                CalendarEventMirror.connection_generation == generation,
            )
            for source, generation in self.account_generations.items()
        )
        return sa.or_(*filters) if filters else sa.false()

    def allows(self, row: CalendarEventMirror) -> bool:
        """Return whether one loaded row belongs to this exact snapshot."""

        generation = self.account_generations.get(row.calendar_source)
        return (
            generation is not None
            and row.connection_generation == generation
        )


class CalendarVisibilityChanged(RuntimeError):
    """The connected Calendar account changed while a consumer was reading."""


def calendar_visibility(
    settings: Settings,
    *,
    sync_health_store: SyncHealthStore | None = None,
) -> CalendarVisibility:
    """Resolve current, successfully synced Calendar account generations."""

    try:
        connected = creds.calendar_account_generations(settings)
    except Exception:
        return CalendarVisibility(
            {},
            ("calendar_connection_state_unavailable",),
        )
    if not connected:
        return CalendarVisibility({}, ("calendar_not_connected",))

    health = (
        sync_health_store
        if sync_health_store is not None
        else FileSyncHealthStore.for_data_dir(settings.data_dir)
    )
    visible: dict[CalendarSource, str] = {}
    limitations: set[str] = set()
    for source, generation in connected.items():
        try:
            state = health.load(source)
        except Exception:
            limitations.add("calendar_sync_health_unavailable")
            continue
        if (
            state is None
            or state.account_generation != generation
            or state.last_success_at is None
        ):
            limitations.add("calendar_account_not_synced")
            continue
        visible[source] = generation
    return CalendarVisibility(
        visible,
        tuple(sorted(limitations)),
    )


def calendar_visibility_is_current(
    settings: Settings,
    visibility: CalendarVisibility,
    *,
    sync_health_store: SyncHealthStore | None = None,
) -> bool:
    """Return whether a previously resolved visibility snapshot is still live."""

    return calendar_visibility(
        settings,
        sync_health_store=sync_health_store,
    ) == visibility


def require_calendar_visibility_current(
    settings: Settings,
    visibility: CalendarVisibility,
    *,
    sync_health_store: SyncHealthStore | None = None,
) -> None:
    """Reject a consumer result when reconnect or first sync raced the read."""

    if not calendar_visibility_is_current(
        settings,
        visibility,
        sync_health_store=sync_health_store,
    ):
        raise CalendarVisibilityChanged(
            "calendar account visibility changed during the read"
        )


def read_visible_calendar[T](
    session: Session,
    settings: Settings,
    reader: Callable[[CalendarVisibility], T],
    *,
    sync_health_store: SyncHealthStore | None = None,
    max_attempts: int = _MAX_STABLE_READ_ATTEMPTS,
) -> tuple[T, CalendarVisibility]:
    """Read visible rows from one stable account-generation snapshot.

    Credential files and sync-health state are outside the SQL transaction.
    Taking every provider write lock would introduce a multi-source lock-order
    cycle. Instead, consumers read only generation-tagged rows and verify the
    same visibility snapshot after the SELECT. A racing reconnect or first
    successful sync discards the result and retries with a fresh transaction.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    for _attempt in range(max_attempts):
        visibility = calendar_visibility(
            settings,
            sync_health_store=sync_health_store,
        )
        result = reader(visibility)
        if calendar_visibility_is_current(
            settings,
            visibility,
            sync_health_store=sync_health_store,
        ):
            return result, visibility
        session.rollback()
        session.expire_all()
    raise CalendarVisibilityChanged(
        "calendar account visibility did not stabilize during the read"
    )
