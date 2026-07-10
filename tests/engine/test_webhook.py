"""Webhook sender tests: signature verified against the vendor's own logic.

``vendor_validate_v2`` below is transcribed 1:1 from the receiving side in
vendor/hermes-agent/gateway/platforms/webhook.py so that these tests fail if
our signing ever drifts from what the Hermes gateway actually verifies.
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from freezegun import freeze_time
from pydantic import SecretStr

from healthmes.config import Settings
from healthmes.engine.webhook import (
    REQUEST_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    HermesWebhookSender,
    build_alert_payload,
    build_alert_prompt,
    sign_v2,
)

FIRED_AT = datetime(2026, 7, 9, 5, 0, tzinfo=UTC)


def vendor_validate_v2(headers: httpx.Headers, body: bytes, secret: str, *, now: int) -> bool:
    """Generic HMAC V2 verification, transcribed from the vendor gateway.

    Source: vendor/hermes-agent/gateway/platforms/webhook.py,
    ``WebhookAdapter._validate_signature`` lines 902-927 —
    ``X-Webhook-Signature-V2`` selects V2 mode; a missing/malformed
    ``X-Webhook-Timestamp`` rejects (no V1 fallback); timestamps outside the
    +/-300s replay window reject; otherwise::

        signed_content = v2_timestamp.encode() + b"." + body
        expected_v2 = hmac.new(secret.encode(), signed_content,
                               hashlib.sha256).hexdigest()
        return hmac.compare_digest(v2_sig, expected_v2)
    """
    v2_sig = headers.get(SIGNATURE_HEADER, "")
    if not v2_sig:
        return False
    v2_timestamp = headers.get(TIMESTAMP_HEADER, "")
    if not v2_timestamp:
        return False
    try:
        ts = int(v2_timestamp)
    except (TypeError, ValueError):
        return False
    if abs(now - ts) > 300:
        return False
    signed_content = v2_timestamp.encode() + b"." + body
    expected_v2 = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v2_sig, expected_v2)


class CapturingTransport:
    """httpx MockTransport handler that records the last request."""

    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json={"status": "accepted"})


@pytest.fixture
def transport() -> CapturingTransport:
    return CapturingTransport()


@pytest.fixture
def sender(settings: Settings, transport: CapturingTransport) -> HermesWebhookSender:
    client = httpx.Client(transport=httpx.MockTransport(transport))
    return HermesWebhookSender(settings, client=client)


@freeze_time("2026-07-09 05:00:00")
def test_post_matches_vendor_route_and_signature(sender, transport, settings, make_fire) -> None:
    fire = make_fire(rule_id="stress_spike_vs_baseline")
    result = sender.send(fire, fired_at=FIRED_AT)

    assert result.ok is True
    assert result.status_code == 202
    [request] = transport.requests
    # URL shape: POST /webhooks/{route_name} (vendor line 206).
    assert request.method == "POST"
    assert str(request.url) == settings.hermes_webhook_url
    assert request.url.path == "/webhooks/healthmes-alerts"
    assert request.headers["Content-Type"] == "application/json"

    # The signature must satisfy the gateway's own verification logic.
    secret = settings.hermes_webhook_secret.get_secret_value()
    assert vendor_validate_v2(request.headers, request.content, secret, now=int(time.time()))

    # Idempotency nonce: stable per dedup_key, and never a svix header
    # (svix-* presence would switch the gateway to Svix validation).
    assert request.headers[REQUEST_ID_HEADER] == f"healthmes:{fire.dedup_key}"
    assert not [name for name in request.headers if name.lower().startswith("svix")]


@freeze_time("2026-07-09 05:00:00")
def test_signature_rejects_tampering(sender, transport, settings, make_fire) -> None:
    sender.send(make_fire(), fired_at=FIRED_AT)
    [request] = transport.requests
    secret = settings.hermes_webhook_secret.get_secret_value()
    now = int(time.time())

    # Tampered body, wrong secret, missing timestamp, expired timestamp.
    assert not vendor_validate_v2(request.headers, request.content + b"x", secret, now=now)
    assert not vendor_validate_v2(request.headers, request.content, "other-secret", now=now)
    stripped = httpx.Headers(
        {k: v for k, v in request.headers.items() if k.lower() != TIMESTAMP_HEADER.lower()}
    )
    assert not vendor_validate_v2(stripped, request.content, secret, now=now)
    assert not vendor_validate_v2(request.headers, request.content, secret, now=now + 301)


@freeze_time("2026-07-09 05:00:00")
def test_signature_covers_exact_sent_bytes(sender, transport, settings, make_fire) -> None:
    sender.send(make_fire(), fired_at=FIRED_AT)
    [request] = transport.requests
    secret = settings.hermes_webhook_secret.get_secret_value()
    timestamp = request.headers[TIMESTAMP_HEADER]
    assert timestamp == str(int(time.time()))
    assert request.headers[SIGNATURE_HEADER] == sign_v2(secret, timestamp, request.content)


def test_payload_fields_feed_route_prompt_template(sender, transport, settings, make_fire) -> None:
    fire = make_fire(rule_id="deadline_risk", dedup_key="deadline_risk:abc123")
    sender.send(fire, fired_at=FIRED_AT)
    [request] = transport.requests
    payload = json.loads(request.content)

    # Flat template-addressable fields used by config/hermes-config.yaml.tmpl
    # ({rule_id}, {summary}, {prompt}) and route event filters.
    assert payload["event_type"] == "healthmes_trigger"
    assert payload["rule_id"] == "deadline_risk"
    assert payload["dedup_key"] == "deadline_risk:abc123"
    assert payload["fired_at"] == FIRED_AT.isoformat()
    assert payload["summary"] == fire.summary
    assert payload["proposal"] == fire.proposal
    assert payload["evidence"] == fire.evidence
    assert payload["decision_link_base"] == "http://healthmes.test:8100/decisions"
    assert payload["prompt"]


def test_prompt_follows_notification_grammar_and_instructs_planner(settings, make_fire) -> None:
    fire = make_fire()
    prompt = build_alert_prompt(
        fire, public_base_url=settings.public_base_url, fired_at=FIRED_AT
    )
    # Notification grammar (docs/PLAN.md section 8.5).
    assert f"Observation: {fire.summary}" in prompt
    assert "Evidence: " in prompt
    assert "recent_value=85" in prompt
    assert f"Proposal: {fire.proposal}" in prompt
    # Skill instruction + decision-detail link from Settings.public_base_url.
    # The link instruction defers to record_decision's viewer_url (it embeds
    # the derived viewer token when API auth is configured).
    assert "healthmes-planner" in prompt
    assert "record_decision" in prompt
    assert "viewer_url" in prompt
    assert "http://healthmes.test:8100/decisions/" in prompt


def test_payload_prompt_matches_builder(settings, make_fire) -> None:
    fire = make_fire()
    payload = build_alert_payload(
        fire, public_base_url=settings.public_base_url, fired_at=FIRED_AT
    )
    assert payload["prompt"] == build_alert_prompt(
        fire, public_base_url=settings.public_base_url, fired_at=FIRED_AT
    )


def test_non_2xx_is_not_ok(settings, make_fire) -> None:
    transport = CapturingTransport(status_code=401)
    client = httpx.Client(transport=httpx.MockTransport(transport))
    result = HermesWebhookSender(settings, client=client).send(make_fire(), fired_at=FIRED_AT)
    assert result.ok is False
    assert result.status_code == 401


def test_transport_error_is_not_ok(settings, make_fire) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(explode))
    result = HermesWebhookSender(settings, client=client).send(make_fire(), fired_at=FIRED_AT)
    assert result.ok is False
    assert result.status_code is None
    assert "connection refused" in (result.detail or "")


def test_missing_secret_fails_closed_without_sending(settings, transport, make_fire) -> None:
    no_secret = settings.model_copy(update={"hermes_webhook_secret": SecretStr("")})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    result = HermesWebhookSender(no_secret, client=client).send(make_fire(), fired_at=FIRED_AT)
    assert result.ok is False
    assert transport.requests == []
