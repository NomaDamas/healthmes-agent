from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

PROOF_VERSION = 1
PROOF_MAX_AGE = dt.timedelta(minutes=5)
PROOF_CLOCK_SKEW = dt.timedelta(seconds=30)


def _encode_payload(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_payload(encoded: str) -> dict[str, Any] | None:
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(encoded + padding)
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def issue_trusted_session_proof(
    secret: str,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    platform: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    issued_at: dt.datetime | None = None,
) -> str:
    if platform != "telegram" or not all((chat_id, user_id, message_id)):
        raise ValueError("trusted session proof requires a live Telegram message")
    now = issued_at or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        raise ValueError("issued_at must be timezone-aware")
    payload = {
        "v": PROOF_VERSION,
        "tool": tool_name,
        "arguments": dict(arguments),
        "platform": platform,
        "chat_id": chat_id,
        "user_id": user_id,
        "message_id": message_id,
        "issued_at": int(now.timestamp()),
    }
    encoded = _encode_payload(payload)
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    return f"{encoded}.{signature.hexdigest()}"


def verify_trusted_session_proof(
    proof: str | None,
    secret: str,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    now: dt.datetime | None = None,
) -> bool:
    if not proof:
        return False
    try:
        encoded, supplied_signature = proof.split(".", 1)
    except ValueError:
        return False
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return False
    payload = _decode_payload(encoded)
    if payload is None:
        return False
    if (
        payload.get("v") != PROOF_VERSION
        or payload.get("tool") != tool_name
        or payload.get("arguments") != dict(arguments)
        or payload.get("platform") != "telegram"
        or not isinstance(payload.get("chat_id"), str)
        or not payload["chat_id"]
        or not isinstance(payload.get("user_id"), str)
        or not payload["user_id"]
        or not isinstance(payload.get("message_id"), str)
        or not payload["message_id"]
    ):
        return False
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int):
        return False
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age = current - dt.datetime.fromtimestamp(issued_at, tz=dt.UTC)
    return -PROOF_CLOCK_SKEW <= age <= PROOF_MAX_AGE
