"""Clocks for deadlines that must ignore wall-clock changes."""

from __future__ import annotations

import time


def steady_time() -> float:
    """Return elapsed seconds from an OS monotonic clock when available."""

    clock_gettime = getattr(time, "clock_gettime", None)
    clock_id = getattr(time, "CLOCK_MONOTONIC", None)
    if clock_gettime is not None and clock_id is not None:
        return clock_gettime(clock_id)
    return time.monotonic()
