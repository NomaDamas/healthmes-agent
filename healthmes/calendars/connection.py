"""Generation-aware calendar backend access under the connection fence."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarAuthError,
    CalendarBackend,
    CalendarError,
)
from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.store.enums import CalendarSource


class CalendarBackendFence:
    """Cache one backend only while its credential generation is current."""

    def __init__(
        self,
        *,
        source: CalendarSource,
        backend_factory: Callable[[], CalendarBackend],
        generation_resolver: Callable[[], str | None],
    ) -> None:
        self._source = source
        self._backend_factory = backend_factory
        self._generation_resolver = generation_resolver
        self._backend: CalendarBackend | None = None
        self._backend_generation: str | None = None

    @property
    def source(self) -> CalendarSource:
        return self._source

    @contextmanager
    def use(self, session: Session) -> Iterator[CalendarBackend]:
        """Yield a current backend while connect/disconnect is excluded."""

        with calendar_write_lock(session, self._source):
            current_generation = self._generation_resolver()
            if current_generation != self._backend_generation:
                self._backend = None
                self._backend_generation = current_generation
            if current_generation is None:
                raise CalendarAuthError(
                    "calendar credentials are not connected"
                )
            if self._backend is None:
                backend = self._backend_factory()
                if backend.source is not self._source:
                    raise CalendarError(
                        "calendar backend source does not match the "
                        "connection fence"
                    )
                confirmed_generation = self._generation_resolver()
                if confirmed_generation != current_generation:
                    self._backend = None
                    self._backend_generation = confirmed_generation
                    raise CalendarAuthError(
                        "calendar credentials changed while building "
                        "the backend"
                    )
                self._backend = backend
            yield self._backend
