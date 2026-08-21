"""Cancellation coordination shared by the decision service and engine."""

from __future__ import annotations

import asyncio
from threading import Lock
from typing import Any


class DecisionExecutionControl:
    """Protect one decision after it crosses the durable finalization boundary."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._finalization_started = False
        self._cancellation_requested = False

    def begin_finalization(self) -> bool:
        """Enter the durable phase unless cancellation won the race."""

        with self._lock:
            if self._cancellation_requested:
                return False
            self._finalization_started = True
            return True

    def cancel_reasoning(self, task: asyncio.Task[Any]) -> bool:
        """Cancel an execution only before durable finalization starts."""

        with self._lock:
            if self._finalization_started:
                return False
            self._cancellation_requested = True
            task.cancel()
            return True
