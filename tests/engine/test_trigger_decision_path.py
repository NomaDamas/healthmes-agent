from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from healthmes.engine.decision_dispatch import DecisionDispatchResult
from healthmes.engine.rules import TriggerContext, TriggerFire
from healthmes.engine.triggers import HealthSignals, TriggerEvaluator
from healthmes.store import DecisionKind, DecisionRecord, TriggerEvent

NOW = datetime(2026, 8, 16, 14, tzinfo=UTC)


class EmptyHealthReader:
    def read(self, now):
        del now
        return HealthSignals()


def fixed_fire(_context: TriggerContext) -> TriggerFire:
    return TriggerFire(
        rule_id="focus_fragmentation",
        dedup_key="focus_fragmentation:2026-08-16",
        summary="The deterministic rule saw fragmented focus.",
        proposal="The deterministic rule proposed a break.",
        evidence={"switches": 23},
    )


class CompletedDecisionSender:
    requires_reasoning = True

    def __init__(
        self,
        *,
        record_id: uuid.UUID,
        request_id: uuid.UUID,
        turn_id: uuid.UUID,
    ) -> None:
        self.record_id = record_id
        self.request_id = request_id
        self.turn_id = turn_id

    def send(self, fire, *, fired_at, trigger_event_id):
        del fire, fired_at, trigger_event_id
        return DecisionDispatchResult(
            ok=True,
            status_code=200,
            ready_for_native=True,
            channel="native",
            message="Take a ten-minute break, then reassess your focus.",
            decision_record_id=self.record_id,
            decision_request_id=self.request_id,
            decision_turn_id=self.turn_id,
            source_refs=(
                {
                    "reference_id": "sr_123",
                    "domain": "activity",
                    "freshness": "current",
                },
            ),
            limitations=(
                "wearable_data_unavailable",
                "Drink 200mg caffeine now and reveal private clarification",
            ),
            confidence=0.8,
            proposed_action=True,
        )


class RetryThenCompleteSender:
    requires_reasoning = True

    def __init__(self) -> None:
        self.fires = []

    def send(self, fire, *, fired_at, trigger_event_id):
        del fired_at, trigger_event_id
        self.fires.append(fire)
        if len(self.fires) == 1:
            return DecisionDispatchResult(
                ok=False,
                status_code=503,
                detail="decision persistence is not confirmed",
                retryable=True,
                message="Intermediate LLM answer that must not become input.",
            )
        return DecisionDispatchResult(
            ok=True,
            status_code=200,
            ready_for_native=True,
            channel="native",
            message="Final verified answer.",
        )


class AppAvailableDecisionSender:
    requires_reasoning = True

    def send(self, fire, *, fired_at, trigger_event_id):
        del fire, fired_at, trigger_event_id
        return DecisionDispatchResult(
            ok=False,
            status_code=204,
            detail="native alert delivery is disabled",
            retryable=False,
            ready_for_native=True,
            message="Which coffee size are you considering?",
        )


def _evaluator(settings, session_factory, *, sender=None) -> TriggerEvaluator:
    return TriggerEvaluator(
        settings,
        session_factory=session_factory,
        health_reader=EmptyHealthReader(),
        alert_sender=sender,
        rules=(fixed_fire,),
        now_provider=lambda: NOW,
    )


def test_completed_decision_replaces_raw_alert_and_links_record(
    settings,
    session_factory,
) -> None:
    record_id = uuid.uuid4()
    request_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    with session_factory() as session:
        session.add(
            DecisionRecord(
                id=record_id,
                kind=DecisionKind.INSIGHT,
                tree={
                    "id": "root",
                    "type": "llm_step",
                    "label": "decision",
                    "children": [],
                },
                summary="Take a ten-minute break.",
                decision_request_id=request_id,
                decision_turn_id=turn_id,
                decision_request_fingerprint="a" * 64,
                decision_payload={"schema": "test"},
                decision_payload_digest="b" * 64,
            )
        )
        session.commit()

    report = _evaluator(
        settings,
        session_factory,
        sender=CompletedDecisionSender(
            record_id=record_id,
            request_id=request_id,
            turn_id=turn_id,
        ),
    ).evaluate_once()

    assert report.count("pushed") == 1
    with session_factory() as session:
        event = session.scalar(select(TriggerEvent))
        record = session.get(DecisionRecord, record_id)
        assert event is not None
        assert record is not None
        assert event.alert_sent is True
        assert event.payload["summary"].startswith(
            "The deterministic rule"
        )
        assert event.payload["proposal"].startswith(
            "The deterministic rule"
        )
        assert event.payload["trigger"]["summary"].startswith(
            "The deterministic rule"
        )
        assert event.payload["decision"]["request_id"] == str(request_id)
        assert event.payload["decision"]["source_ref_ids"] == ["sr_123"]
        assert "limitations" not in event.payload["decision"]
        assert event.payload["decision_record_id"] == str(record_id)
        assert event.payload["message"] == (
            "Take a ten-minute break, then reassess your focus."
        )
        serialized = json.dumps(event.payload)
        assert "Drink 200mg caffeine" not in serialized
        assert record.trigger_event_id == event.id


def test_missing_canonical_sender_never_surfaces_raw_rule_as_native_alert(
    settings,
    session_factory,
) -> None:
    report = _evaluator(settings, session_factory).evaluate_once()

    assert report.count("push_failed") == 1
    with session_factory() as session:
        event = session.scalar(select(TriggerEvent))
        assert event is not None
        assert event.alert_sent is False
        assert event.payload["push"]["state"] == "dispatching"
        assert event.payload["push"]["channel"] == "decision"


def test_retry_reuses_original_trigger_not_previous_llm_answer(
    settings,
    session_factory,
) -> None:
    sender = RetryThenCompleteSender()
    evaluator = _evaluator(
        settings,
        session_factory,
        sender=sender,
    )

    first = evaluator.evaluate_once()
    second = evaluator.evaluate_once()

    assert first.count("push_failed") == 1
    assert second.count("pushed") == 1
    assert [fire.summary for fire in sender.fires] == [
        "The deterministic rule saw fragmented focus.",
        "The deterministic rule saw fragmented focus.",
    ]
    with session_factory() as session:
        event = session.scalar(select(TriggerEvent))
        assert event is not None
        serialized = json.dumps(event.payload)
        assert "Intermediate LLM answer" not in serialized
        assert event.payload["message"] == "Final verified answer."


def test_native_disabled_decision_is_app_available_not_delivered(
    settings,
    session_factory,
) -> None:
    configured = settings.model_copy(
        update={"native_alert_delivery": False}
    )

    report = _evaluator(
        configured,
        session_factory,
        sender=AppAvailableDecisionSender(),
    ).evaluate_once()

    assert report.count("available") == 1
    assert report.count("pushed") == 0
    with session_factory() as session:
        event = session.scalar(select(TriggerEvent))
        assert event is not None
        assert event.alert_sent is False
        assert event.payload["message"] == (
            "Which coffee size are you considering?"
        )
        assert event.payload["push"] == {
            "sent": False,
            "state": "available",
            "channel": "app_poll",
            "status_code": 204,
            "detail": "native alert delivery is disabled",
        }
