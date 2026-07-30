import datetime as dt

import pytest

from healthmes.trusted_session import (
    issue_trusted_session_proof,
    verify_trusted_session_proof,
)

SECRET = "trusted-session-test-secret-at-least-32-characters"
ARGUMENTS = {
    "proposal_id": "proposal-1",
    "action": "accept",
    "reply_handle": "handle-1",
}
NOW = dt.datetime(2026, 7, 30, 1, 0, tzinfo=dt.UTC)


def _proof(**overrides) -> str:
    values = {
        "tool_name": "resolve_schedule_proposal",
        "arguments": ARGUMENTS,
        "platform": "telegram",
        "chat_id": "chat-1",
        "user_id": "user-1",
        "message_id": "message-1",
        "issued_at": NOW,
        **overrides,
    }
    return issue_trusted_session_proof(SECRET, **values)


def test_exact_fresh_telegram_call_verifies() -> None:
    assert verify_trusted_session_proof(
        _proof(),
        SECRET,
        tool_name="resolve_schedule_proposal",
        arguments=ARGUMENTS,
        now=NOW + dt.timedelta(minutes=4),
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "now"),
    [
        ("resolve_calendar_adjustment", ARGUMENTS, NOW),
        (
            "resolve_schedule_proposal",
            {**ARGUMENTS, "action": "decline"},
            NOW,
        ),
        (
            "resolve_schedule_proposal",
            ARGUMENTS,
            NOW + dt.timedelta(minutes=6),
        ),
    ],
)
def test_mismatched_or_expired_call_is_rejected(
    tool_name: str,
    arguments: dict,
    now: dt.datetime,
) -> None:
    assert not verify_trusted_session_proof(
        _proof(),
        SECRET,
        tool_name=tool_name,
        arguments=arguments,
        now=now,
    )


def test_non_telegram_origin_cannot_issue_proof() -> None:
    with pytest.raises(ValueError, match="live Telegram"):
        _proof(platform="cron")


def test_tampered_proof_is_rejected() -> None:
    proof = _proof()
    assert not verify_trusted_session_proof(
        f"{proof[:-1]}0",
        SECRET,
        tool_name="resolve_schedule_proposal",
        arguments=ARGUMENTS,
        now=NOW,
    )
