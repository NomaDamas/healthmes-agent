import datetime as dt

import pytest

from healthmes.trusted_session import (
    issue_trusted_session_proof,
    verify_trusted_session_proof,
)

SECRET = "trusted-session-test-secret-at-least-32-characters"
ARGUMENTS = {
    "response": "적용 handle-1",
    "reply_handle": "handle-1",
}
NOW = dt.datetime(2026, 8, 3, 1, 0, tzinfo=dt.UTC)


def _proof(**overrides) -> str:
    values = {
        "tool_name": "resolve_calendar_adjustment",
        "arguments": ARGUMENTS,
        "platform": "telegram",
        "chat_id": "owner-chat",
        "user_id": "owner-user",
        "message_id": "message-1",
        "issued_at": NOW,
        **overrides,
    }
    return issue_trusted_session_proof(SECRET, **values)


def _verify(proof: str, **overrides):
    values = {
        "tool_name": "resolve_calendar_adjustment",
        "arguments": ARGUMENTS,
        "expected_user_id": "owner-user",
        "expected_chat_id": "owner-chat",
        "now": NOW + dt.timedelta(minutes=4),
        **overrides,
    }
    return verify_trusted_session_proof(proof, SECRET, **values)


def test_exact_fresh_owner_telegram_call_verifies() -> None:
    claims = _verify(_proof())

    assert claims is not None
    assert claims.user_id == "owner-user"
    assert claims.chat_id == "owner-chat"
    assert claims.message_id == "message-1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_name": "other_tool"},
        {"arguments": {**ARGUMENTS, "response": "그대로 handle-1"}},
        {"expected_user_id": "different-user"},
        {"expected_chat_id": "different-chat"},
        {"now": NOW + dt.timedelta(minutes=6)},
    ],
)
def test_mismatched_expired_or_non_owner_call_is_rejected(overrides) -> None:
    assert _verify(_proof(), **overrides) is None


def test_non_telegram_origin_cannot_issue_proof() -> None:
    with pytest.raises(ValueError, match="live Telegram"):
        _proof(platform="discord")


def test_tampered_proof_is_rejected() -> None:
    proof = _proof()
    assert _verify(f"{proof[:-1]}0") is None
