import datetime as dt
from types import SimpleNamespace

from healthmes.trusted_session import verify_trusted_session_proof


def test_vendor_live_reply_proof_verifies_at_healthmes_boundary(
    vendor_cron,
    monkeypatch,
) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.mcp_tool import _trusted_session_call_arguments

    secret = "cross-runtime-test-secret-at-least-32-characters"
    server = SimpleNamespace(
        _config={
            "trusted_session_proof": {
                "secret_env": "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET",
                "argument": "trusted_session_proof",
                "confirmations": {
                    "resolve_schedule_proposal": {
                        "handle_argument": "reply_handle",
                        "action_argument": "action",
                        "bind_arguments": [
                            "proposal_id",
                            "action",
                            "reply_handle",
                        ],
                        "choices": {
                            "accept": "적용",
                            "decline": "그대로",
                        },
                    }
                },
            }
        }
    )
    arguments = {
        "proposal_id": "proposal-1",
        "action": "accept",
        "reply_handle": "handle-1",
    }
    monkeypatch.setenv("HEALTHMES_CALENDAR_ADJUSTMENT_SECRET", secret)
    tokens = set_session_vars(
        platform="telegram",
        chat_id="chat-1",
        user_id="user-1",
        message_id="message-1",
        message_text="적용 handle-1",
    )
    try:
        signed = _trusted_session_call_arguments(
            server,
            "resolve_schedule_proposal",
            arguments,
        )
    finally:
        clear_session_vars(tokens)

    assert verify_trusted_session_proof(
        signed["trusted_session_proof"],
        secret,
        tool_name="resolve_schedule_proposal",
        arguments=arguments,
        now=dt.datetime.now(dt.UTC),
    )


def test_vendor_cron_turn_cannot_mint_confirmation_proof(
    vendor_cron,
    monkeypatch,
) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.mcp_tool import _trusted_session_call_arguments

    server = SimpleNamespace(
        _config={
            "trusted_session_proof": {
                "secret_env": "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET",
                "confirmations": {
                    "resolve_schedule_proposal": {
                        "handle_argument": "reply_handle",
                        "action_argument": "action",
                        "bind_arguments": [
                            "proposal_id",
                            "action",
                            "reply_handle",
                        ],
                        "choices": {"accept": "적용"},
                    }
                },
            }
        }
    )
    arguments = {
        "proposal_id": "proposal-1",
        "action": "accept",
        "reply_handle": "handle-1",
    }
    monkeypatch.setenv(
        "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET",
        "cross-runtime-test-secret-at-least-32-characters",
    )
    tokens = set_session_vars(
        platform="telegram",
        chat_id="chat-1",
        message_text="적용 handle-1",
    )
    try:
        unsigned = _trusted_session_call_arguments(
            server,
            "resolve_schedule_proposal",
            arguments,
        )
    finally:
        clear_session_vars(tokens)

    assert unsigned == arguments


def test_proof_secret_is_removed_from_model_subprocesses(vendor_cron) -> None:
    from tools.environments.local import _sanitize_subprocess_env

    sanitized = _sanitize_subprocess_env(
        {
            "PATH": "/usr/bin",
            "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET": (
                "cross-runtime-test-secret-at-least-32-characters"
            ),
        }
    )

    assert sanitized["PATH"] == "/usr/bin"
    assert "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET" not in sanitized
