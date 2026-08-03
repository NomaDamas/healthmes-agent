import datetime as dt
from types import SimpleNamespace

from healthmes.trusted_session import verify_trusted_session_proof


def _server() -> SimpleNamespace:
    return SimpleNamespace(
        _config={
            "trusted_session_proof": {
                "secret_env": "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET",
                "argument": "trusted_session_proof",
                "owner_user_id": "owner-user",
                "owner_chat_id": "owner-chat",
                "confirmations": {
                    "resolve_calendar_adjustment": {
                        "handle_argument": "reply_handle",
                        "passthrough_argument": "response",
                        "bind_arguments": ["response", "reply_handle"],
                        "choices": ["적용", "그대로"],
                    }
                },
            }
        }
    )


def test_vendor_live_owner_reply_proof_verifies_at_healthmes_boundary(
    vendor_cron,
    monkeypatch,
) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.mcp_tool import _trusted_session_call_arguments

    secret = "cross-runtime-test-secret-at-least-32-characters"
    arguments = {
        "response": "적용 handle-1",
        "reply_handle": "handle-1",
    }
    monkeypatch.setenv("HEALTHMES_CALENDAR_ADJUSTMENT_SECRET", secret)
    tokens = set_session_vars(
        platform="telegram",
        chat_id="owner-chat",
        user_id="owner-user",
        message_id="message-1",
        message_text="적용 handle-1",
    )
    try:
        signed = _trusted_session_call_arguments(
            _server(),
            "resolve_calendar_adjustment",
            arguments,
        )
    finally:
        clear_session_vars(tokens)

    assert (
        verify_trusted_session_proof(
            signed["trusted_session_proof"],
            secret,
            tool_name="resolve_calendar_adjustment",
            arguments=arguments,
            expected_user_id="owner-user",
            expected_chat_id="owner-chat",
            now=dt.datetime.now(dt.UTC),
        )
        is not None
    )


def test_vendor_non_owner_or_cron_turn_cannot_mint_confirmation_proof(
    vendor_cron,
    monkeypatch,
) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.mcp_tool import _trusted_session_call_arguments

    arguments = {
        "response": "적용 handle-1",
        "reply_handle": "handle-1",
    }
    monkeypatch.setenv(
        "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET",
        "cross-runtime-test-secret-at-least-32-characters",
    )
    sessions = [
        {
            "platform": "telegram",
            "chat_id": "owner-chat",
            "user_id": "different-user",
            "message_id": "message-1",
            "message_text": "적용 handle-1",
        },
        {
            "platform": "discord",
            "chat_id": "owner-chat",
            "user_id": "owner-user",
            "message_id": "message-1",
            "message_text": "적용 handle-1",
        },
        {
            "platform": "telegram",
            "chat_id": "owner-chat",
            "message_text": "적용 handle-1",
        },
    ]
    for session in sessions:
        tokens = set_session_vars(**session)
        try:
            unsigned = _trusted_session_call_arguments(
                _server(),
                "resolve_calendar_adjustment",
                arguments,
            )
        finally:
            clear_session_vars(tokens)
        assert unsigned == arguments


def test_proof_secret_is_removed_from_model_subprocesses(
    vendor_cron,
    monkeypatch,
) -> None:
    from tools.environments.local import (
        _sanitize_subprocess_env,
        hermes_subprocess_env,
    )

    secret = "cross-runtime-test-secret-at-least-32-characters"
    sanitized = _sanitize_subprocess_env(
        {
            "PATH": "/usr/bin",
            "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET": secret,
        }
    )

    assert sanitized["PATH"] == "/usr/bin"
    assert "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET" not in sanitized

    monkeypatch.setenv("HEALTHMES_CALENDAR_ADJUSTMENT_SECRET", secret)
    inherited = hermes_subprocess_env(inherit_credentials=True)
    assert "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET" not in inherited
