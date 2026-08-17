from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from healthmes.decision import (
    DecisionResult,
    DecisionStatus,
    PersistenceStatus,
    RuntimeMetadata,
)
from healthmes.engine.decision_dispatch import (
    DecisionAlertSender,
    DecisionServiceThreadBridge,
)
from healthmes.engine.rules import TriggerFire

NOW = datetime(2026, 8, 16, 8, tzinfo=UTC)
TRIGGER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class RecordingBridge:
    def __init__(
        self,
        *,
        status: DecisionStatus = DecisionStatus.COMPLETED,
        proposed_action: bool = False,
        persistence_status: PersistenceStatus = (
            PersistenceStatus.NOT_REQUIRED
        ),
    ):
        self.status = status
        self.proposed_action = proposed_action
        self.persistence_status = persistence_status
        self.submissions = []

    def ask_wellness(self, submission):
        self.submissions.append(submission)
        if self.status is DecisionStatus.COMPLETED:
            values = {
                "request_id": submission.request_id,
                "turn_id": uuid.uuid4(),
                "status": DecisionStatus.COMPLETED,
                "answer": (
                    "Take a short break before deciding what to do next."
                ),
                "proposed_action": self.proposed_action,
                "persistence_status": self.persistence_status,
                "decision_record_id": (
                    uuid.uuid4()
                    if self.persistence_status
                    is PersistenceStatus.PERSISTED
                    else None
                ),
                "runtime": RuntimeMetadata(runtime="test"),
            }
            if (
                self.proposed_action
                and self.persistence_status
                is not PersistenceStatus.PERSISTED
            ):
                # Exercise the delivery boundary against a malformed adapter
                # result that the canonical finalizer would never emit.
                return DecisionResult.model_construct(**values)
            return DecisionResult(**values)
        if self.status is DecisionStatus.NEEDS_CLARIFICATION:
            return DecisionResult(
                request_id=submission.request_id,
                turn_id=uuid.uuid4(),
                status=self.status,
                clarification_question="Which coffee size are you considering?",
                persistence_status=PersistenceStatus.NOT_REQUIRED,
                runtime=RuntimeMetadata(runtime="test"),
            )
        return DecisionResult(
            request_id=submission.request_id,
            turn_id=uuid.uuid4(),
            status=self.status,
            persistence_status=PersistenceStatus.NOT_REQUIRED,
            runtime=RuntimeMetadata(runtime="test"),
        )


def _fire(rule_id: str = "focus_fragmentation") -> TriggerFire:
    return TriggerFire(
        rule_id=rule_id,
        dedup_key=f"{rule_id}:2026-08-16",
        summary="Focus was fragmented.",
        proposal="Consider a short break.",
        evidence={"switches": 23},
    )


def test_retry_uses_stable_request_id_and_canonical_proactive_ingress(
    settings,
) -> None:
    bridge = RecordingBridge()
    sender = DecisionAlertSender(settings, bridge=bridge)

    first = sender.send(_fire(), fired_at=NOW, trigger_event_id=TRIGGER_ID)
    second = sender.send(_fire(), fired_at=NOW, trigger_event_id=TRIGGER_ID)

    assert first.ok is False
    assert second.ok is False
    assert first.ready_for_native is True
    assert first.channel == "app_poll"
    assert bridge.submissions[0].request_id == bridge.submissions[1].request_id
    assert bridge.submissions[0].ingress.value == "proactive"
    assert bridge.submissions[0].source == "focus_fragmentation"
    assert bridge.submissions[0].persistence_requested is False
    assert bridge.submissions[0].session_id == f"trigger-event:{TRIGGER_ID}"


def test_scheduled_rule_uses_scheduled_ingress(settings) -> None:
    bridge = RecordingBridge()
    sender = DecisionAlertSender(settings, bridge=bridge)

    result = sender.send(
        _fire("scheduled_briefing.morning"),
        fired_at=NOW,
        trigger_event_id=TRIGGER_ID,
    )

    assert result.ok is False
    assert result.ready_for_native is True
    assert bridge.submissions[0].ingress.value == "scheduled"
    assert bridge.submissions[0].source == "morning"


def test_failed_reasoning_is_not_ready_for_native_fallback(settings) -> None:
    sender = DecisionAlertSender(
        settings,
        bridge=RecordingBridge(status=DecisionStatus.FAILED),
    )

    result = sender.send(
        _fire(),
        fired_at=NOW,
        trigger_event_id=TRIGGER_ID,
    )

    assert result.ok is False
    assert result.retryable is True
    assert result.ready_for_native is False
    assert result.message is None


def test_simple_proactive_summary_is_deliverable_without_record(
    settings,
) -> None:
    sender = DecisionAlertSender(
        settings,
        bridge=RecordingBridge(
            proposed_action=False,
            persistence_status=PersistenceStatus.NOT_REQUIRED,
        ),
    )

    result = sender.send(
        _fire(),
        fired_at=NOW,
        trigger_event_id=TRIGGER_ID,
    )

    assert result.ok is False
    assert result.ready_for_native is True
    assert result.decision_record_id is None


def test_proactive_clarification_is_deliverable_as_user_message(
    settings,
) -> None:
    sender = DecisionAlertSender(
        settings,
        bridge=RecordingBridge(
            status=DecisionStatus.NEEDS_CLARIFICATION,
        ),
    )

    result = sender.send(
        _fire(),
        fired_at=NOW,
        trigger_event_id=TRIGGER_ID,
    )

    assert result.ok is False
    assert result.ready_for_native is True
    assert result.message == "Which coffee size are you considering?"


def test_proactive_action_without_confirmed_record_is_retried(
    settings,
) -> None:
    sender = DecisionAlertSender(
        settings,
        bridge=RecordingBridge(
            proposed_action=True,
            persistence_status=PersistenceStatus.FAILED,
        ),
    )

    result = sender.send(
        _fire(),
        fired_at=NOW,
        trigger_event_id=TRIGGER_ID,
    )

    assert result.ok is False
    assert result.retryable is True
    assert result.ready_for_native is False
    assert result.detail == "decision persistence is not confirmed"


def test_native_delivery_disabled_suppresses_generated_message(
    settings,
) -> None:
    configured = settings.model_copy(
        update={"native_alert_delivery": False}
    )
    sender = DecisionAlertSender(
        configured,
        bridge=RecordingBridge(),
    )

    result = sender.send(
        _fire(),
        fired_at=NOW,
        trigger_event_id=TRIGGER_ID,
    )

    assert result.ok is False
    assert result.retryable is False
    assert result.ready_for_native is False
    assert result.channel is None
    assert result.message is None
    assert result.suppressed_reason == (
        "native_alert_delivery_disabled"
    )
    assert result.detail == "native alert delivery is disabled"


@pytest.mark.asyncio
async def test_thread_bridge_runs_service_on_application_loop() -> None:
    class AsyncService:
        async def ask_wellness(self, submission):
            await asyncio.sleep(0)
            return submission

    loop = asyncio.get_running_loop()
    bridge = DecisionServiceThreadBridge(
        service=AsyncService(),
        loop=loop,
        timeout_seconds=1,
    )
    submission = object()

    result = await asyncio.to_thread(
        bridge.ask_wellness,
        submission,
    )

    assert result is submission
