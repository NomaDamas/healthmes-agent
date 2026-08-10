"""Shared timezone parsing for HealthMes domain modules."""

from __future__ import annotations

import re
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FIXED_UTC_OFFSET = re.compile(
    r"^UTC(?P<sign>[+-])(?P<hours>(?:[01]\d|2[0-3])):"
    r"(?P<minutes>[0-5]\d)$"
)


def is_fixed_offset_timezone_name(value: object) -> bool:
    return isinstance(value, str) and _FIXED_UTC_OFFSET.fullmatch(value) is not None


def parse_timezone(value: str | tzinfo) -> tzinfo:
    """Parse an IANA zone or the stable ``UTC+09:00`` form of ``tzinfo``."""
    if not isinstance(value, str):
        return value
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        match = _FIXED_UTC_OFFSET.fullmatch(value)
        if match is None:
            raise ValueError(f"invalid timezone: {value!r}") from exc
        offset = timedelta(
            hours=int(match.group("hours")),
            minutes=int(match.group("minutes")),
        )
        if match.group("sign") == "-":
            offset = -offset
        return timezone(offset, name=value)
