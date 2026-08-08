"""Shared integrity helpers for durable nutrition operation results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

RESULT_PAYLOAD_DIGEST_FIELD = "result_payload_sha256"


def result_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 digest for a stored result payload."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
