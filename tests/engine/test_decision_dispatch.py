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
    def __init__(self, *, status: DecisionStatus = DecisionStatus.COMPLETED):
        self.status = status
        self.submissions = []

    def ask_wellness(self, submission):
        self.submissions.append(submission)
        if self.status is DecisionStatus.COMPLETED:
            return DecisionResult(
                request_id=submission.request_id,
                turn_id=uuid.uuid4(),
                status=DecisionStatus.COMPLETED,
                answer="Take a short break before deciding what to do next.",
                persistence_status=PersistenceStatus.PERSISTED,
                decision_record_id=uuid.uuid4(),
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

    assert first.ok is True
    assert second.ok is True
    assert bridge.submissions[0].request_id == bridge.submissions[1].request_id
    assert bridge.submissions[0].ingress.value == "proactive"
    assert bridge.submissions[0].source == "focus_fragmentation"
    assert bridge.submissions[0].persistence_requested is True
    assert bridge.submissions[0].session_id == f"trigger-event:{TRIGGER_ID}"


def test_scheduled_rule_uses_scheduled_ingress(settings) -> None:
    bridge = RecordingBridge()
    sender = DecisionAlertSender(settings, bridge=bridge)

    result = sender.send(
        _fire("scheduled_briefing.morning"),
        fired_at=NOW,
        trigger_event_id=TRIGGER_ID,
    )

    assert result.ok is True
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
