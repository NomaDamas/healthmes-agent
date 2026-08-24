"""Process clock seam for deterministic runtime and test time."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(UTC)
