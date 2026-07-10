"""Small shared pieces for the REST layer: UTC datetime normalisation.

The store's DateTime columns are timezone-aware on postgres but sqlite keeps
no offset, so mixed naive/aware values would corrupt comparisons (both in SQL
string comparisons on sqlite and in Python ``<``). The rule everywhere in the
API layer: **normalise to aware UTC at the boundary** — request bodies and
query params run through :func:`ensure_utc`, and values read back from sqlite
(naive) are re-interpreted as UTC before any Python-side comparison.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator

__all__ = ["ensure_utc", "utc_now", "UTCDateTime"]


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime (naive values are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Current aware UTC time (single seam for tests via freezegun)."""
    return datetime.now(UTC)


# Pydantic field type: any incoming datetime is normalised to aware UTC.
UTCDateTime = Annotated[datetime, AfterValidator(ensure_utc)]
