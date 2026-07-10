"""Proactive alert push to the Hermes gateway webhook platform.

The receiving contract is ``vendor/hermes-agent/gateway/platforms/webhook.py``
(WebhookAdapter); everything here is written against that file:

- **URL**: ``POST /webhooks/{route_name}`` (route registration, vendor line
  206); the default gateway port is 8644 (``DEFAULT_PORT``, line 77). The full
  URL comes from ``Settings.hermes_webhook_url`` and the route name
  (``healthmes-alerts``) is configured in ``config/hermes-config.yaml.tmpl``.
- **Auth**: generic HMAC **V2** (vendor lines 902-927):
  ``X-Webhook-Signature-V2`` = hex HMAC-SHA256 over the byte string
  ``b"{timestamp}.{body}"`` keyed with the UTF-8 shared secret, plus
  ``X-Webhook-Timestamp`` = unix seconds. The gateway enforces a +/-300s
  replay window and rejects V2 signatures without a timestamp. The legacy
  body-only V1 header is deprecated upstream and never sent here.
- **Idempotency nonce**: ``X-Request-ID`` is the delivery id the gateway
  dedupes on (vendor lines 609-628; precedence X-GitHub-Delivery > svix-id >
  X-Request-ID). We must NOT send any ``svix-*`` header: its mere presence
  switches signature validation to the Svix scheme (vendor lines 861-871).
- **Payload**: the gateway renders the route's ``prompt`` template against
  this JSON body via ``{dot.path}`` lookups / ``{__raw__}`` (``_render_prompt``,
  vendor lines 1013-1051), injects the route's ``skills``
  (``healthmes-planner``) and delivers the agent's answer via the route's
  ``deliver`` target (telegram). The payload therefore carries flat,
  template-addressable fields (``rule_id``, ``summary``, ``prompt``, ...).
  Note the sizing footgun: ``{__raw__}`` dumps the payload as indent-2 JSON
  truncated at 4000 chars (vendor line 1040), which could clip evidence —
  the default route template therefore substitutes ``{prompt}`` (plain
  string substitution, no cap) instead of ``{__raw__}``.

The alert ``prompt`` follows the notification grammar of docs/PLAN.md
section 8.5 (observation / evidence / proposal) and instructs the agent to
follow the ``healthmes-planner`` skill and to append a decision-detail link
built from ``Settings.public_base_url``.
"""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from healthmes.config import Settings
from healthmes.engine.rules import TriggerFire

__all__ = [
    "WebhookResult",
    "HermesWebhookSender",
    "sign_v2",
    "build_alert_prompt",
    "build_alert_payload",
]

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10.0
SIGNATURE_HEADER = "X-Webhook-Signature-V2"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
REQUEST_ID_HEADER = "X-Request-ID"


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """Outcome of one push attempt (``ok`` gates ``trigger_event.alert_sent``)."""

    ok: bool
    status_code: int | None = None
    detail: str | None = None


def sign_v2(secret: str, timestamp: str, body: bytes) -> str:
    """Compute the generic HMAC V2 signature the Hermes gateway verifies.

    Mirror of the verification in vendor/hermes-agent/gateway/platforms/
    webhook.py lines 923-927::

        signed_content = v2_timestamp.encode() + b"." + body
        expected_v2 = hmac.new(secret.encode(), signed_content,
                               hashlib.sha256).hexdigest()
    """
    signed_content = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed_content, hashlib.sha256).hexdigest()


def _compact(value: Any) -> str:
    """One-line rendering of an evidence value."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _format_evidence(evidence: dict[str, Any]) -> str:
    return "; ".join(f"{key}={_compact(value)}" for key, value in evidence.items())


def build_alert_prompt(fire: TriggerFire, *, public_base_url: str, fired_at: datetime) -> str:
    """Build the agent instruction for one trigger fire.

    Structured around the notification grammar (docs/PLAN.md section 8.5):
    one observation line, one evidence line, one proposal — and it tells the
    agent to answer the user in exactly that shape, with quick-reply choices
    and a decision-detail link under ``public_base_url``.
    """
    decision_link_base = public_base_url.rstrip("/") + "/decisions"
    return (
        f"HealthMes deterministic trigger '{fire.rule_id}' fired at "
        f"{fired_at.isoformat()}. Act as the proactive health-aware planner: "
        f"follow the healthmes-planner skill procedure.\n"
        f"\n"
        f"Observation: {fire.summary}\n"
        f"Evidence: {_format_evidence(fire.evidence)}\n"
        f"Proposal: {fire.proposal}\n"
        f"\n"
        f"Steps:\n"
        f"1. Verify the observation with the healthmes and open_wearables MCP "
        f"tools (do not re-derive raw data; use the interpreted context "
        f"tools).\n"
        f"2. Record your reasoning with the healthmes record_decision MCP "
        f"tool (kind='alert'); it returns a decision id.\n"
        f"3. Send the user exactly ONE concise message in the standard "
        f"notification grammar: one observation line, one evidence line, one "
        f"proposal line, then the quick choices 'apply / adjust / keep as "
        f"is', and end with the decision-detail link: use the viewer_url "
        f"returned by record_decision verbatim (it points under "
        f"{decision_link_base}/ and already carries any required access "
        f"token).\n"
        f"If the evidence does not hold up on verification, send nothing and "
        f"record why in the decision."
    )


def build_alert_payload(
    fire: TriggerFire, *, public_base_url: str, fired_at: datetime
) -> dict[str, Any]:
    """JSON payload for the gateway route (template-addressable flat fields).

    The default route prompt in ``config/hermes-config.yaml.tmpl`` reads
    ``{rule_id}`` / ``{summary}`` / ``{prompt}`` — string placeholders render
    in full, whereas ``{__raw__}`` would truncate the indent-2 payload dump
    at 4000 chars (vendor ``_render_prompt``) and could clip ``evidence`` on
    large fires. ``event_type`` feeds the route's optional ``events`` filter
    (vendor lines 554-563).
    """
    return {
        "event_type": "healthmes_trigger",
        "rule_id": fire.rule_id,
        "dedup_key": fire.dedup_key,
        "fired_at": fired_at.isoformat(),
        "summary": fire.summary,
        "proposal": fire.proposal,
        "evidence": fire.evidence,
        "decision_link_base": public_base_url.rstrip("/") + "/decisions",
        "prompt": build_alert_prompt(fire, public_base_url=public_base_url, fired_at=fired_at),
    }


class HermesWebhookSender:
    """HMAC-signed POST of trigger fires to the Hermes webhook route.

    An injected ``client`` (e.g. httpx.MockTransport-backed) is used as-is
    and never closed here; otherwise a short-lived client is created per
    send — pushes are rare (rule fires), so connection reuse is irrelevant.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def send(self, fire: TriggerFire, *, fired_at: datetime) -> WebhookResult:
        """POST one fire; True only on a 2xx gateway response.

        The gateway answers 202 (accepted, agent run scheduled) or 200 with
        ``status: duplicate`` when our delivery id was already processed —
        both count as delivered. Signature is computed over the exact bytes
        sent, per the vendor verification.
        """
        secret = self._settings.hermes_webhook_secret.get_secret_value()
        if not secret:
            logger.error(
                "Hermes webhook secret is not configured "
                "(HEALTHMES_HERMES_WEBHOOK_SECRET); alert push skipped."
            )
            return WebhookResult(ok=False, detail="webhook secret not configured")

        payload = build_alert_payload(
            fire, public_base_url=self._settings.public_base_url, fired_at=fired_at
        )
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign_v2(secret, timestamp, body),
            # Delivery id for the gateway's idempotency cache; stable per
            # dedup_key so accidental double-sends collapse gateway-side too.
            REQUEST_ID_HEADER: f"healthmes:{fire.dedup_key}",
        }

        url = self._settings.hermes_webhook_url
        try:
            if self._client is not None:
                response = self._client.post(url, content=body, headers=headers)
            else:
                with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Hermes webhook push failed for %s: %s", fire.rule_id, exc)
            return WebhookResult(ok=False, detail=str(exc))

        ok = response.is_success
        if not ok:
            logger.warning(
                "Hermes webhook rejected %s: HTTP %s %s",
                fire.rule_id,
                response.status_code,
                response.text[:200],
            )
        return WebhookResult(ok=ok, status_code=response.status_code, detail=response.text[:200])
