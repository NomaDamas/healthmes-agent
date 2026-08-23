"""Canonical source-provider identity shared by wellness writers."""

from __future__ import annotations

import re

SOURCE_PROVIDER_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
_SOURCE_PROVIDER_RE = re.compile(SOURCE_PROVIDER_PATTERN)
_SOURCE_PROVIDER_ALLOWED = "abcdefghijklmnopqrstuvwxyz0123456789._-"
_SOURCE_PROVIDER_FIRST_ALLOWED = "abcdefghijklmnopqrstuvwxyz0123456789"
SOURCE_PROVIDER_MAX_LENGTH = 64


def _allowed_character_count_sql(
    column: str,
    *,
    allowed: str = _SOURCE_PROVIDER_ALLOWED,
) -> str:
    terms = [
        (
            f"(length({column}) - "
            f"length(replace({column}, '{character}', '')))"
        )
        for character in allowed
    ]
    # A flat sum avoids SQLite's parser-stack overflow from deeply nested
    # replace(replace(...)) expressions while preserving the same validation.
    return " + ".join(terms) if terms else "0"


def _contains_only_sql(
    column: str,
    *,
    allowed: str = _SOURCE_PROVIDER_ALLOWED,
) -> str:
    return (
        f"({_allowed_character_count_sql(column, allowed=allowed)}) "
        f"= length({column})"
    )


def _source_provider_check_expression(column: str) -> str:
    first_character = f"substr({column}, 1, 1)"
    return (
        f"{column} = trim({column}) "
        f"AND length({column}) BETWEEN 1 AND 64 "
        f"AND {column} = substr({column}, 1, 64) "
        f"AND {_contains_only_sql(
            first_character,
            allowed=_SOURCE_PROVIDER_FIRST_ALLOWED,
        )} "
        f"AND {_contains_only_sql(column)}"
    )


SOURCE_PROVIDER_CHECK_EXPRESSION = _source_provider_check_expression(
    "source_provider"
)
RAW_INGEST_SOURCE_CHECK_EXPRESSION = _source_provider_check_expression(
    "source"
)


def canonical_source_provider(value: object) -> str:
    """Return one portable ASCII provider identity."""

    if type(value) is not str:
        raise ValueError("source_provider must be a string")
    stripped = value.strip(" ")
    if not stripped.isascii():
        raise ValueError(
            "source_provider must be an ASCII identifier containing only "
            "letters, digits, '.', '_', or '-'"
        )
    canonical = stripped.lower()
    if (
        len(canonical) > SOURCE_PROVIDER_MAX_LENGTH
        or _SOURCE_PROVIDER_RE.fullmatch(canonical) is None
    ):
        raise ValueError(
            "source_provider must be an ASCII identifier containing only "
            "letters, digits, '.', '_', or '-', with at most 64 characters"
        )
    return canonical
