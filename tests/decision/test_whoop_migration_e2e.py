from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import healthmes.decision.domain_providers as domain_providers
from healthmes.decision import (
    ContextAccessLayer,
    ContextProviderRegistry,
    DatabaseDecisionPolicyResolver,
    DecisionActionKind,
    DecisionActionState,
    DecisionChannelAdapter,
    DecisionChannelRequest,
    DecisionContextHints,
    DecisionContextSearchSessionService,
    DecisionRecordSummaryCode,
    DecisionStatus,
    ExecutionScope,
    HealthMesDecisionService,
    HermesResponsesHttpResult,
    PersistenceStatus,
    WearableContextProvider,
    build_healthmes_responses_decision_engine,
    decision_record_summary,
    ensure_decision_domain_policies,
)
from healthmes.store import (
    Base,
    DecisionRecord,
    WellnessEvent,
    create_db_engine,
)
from healthmes.wearables.availability import (
    OpenWearablesAvailabilitySnapshot,
    OpenWearablesAvailabilityState,
    OpenWearablesCapabilityBinding,
    OpenWearablesProviderBinding,
    OpenWearablesProviderSourceBinding,
)
from healthmes.wearables.provenance import (
    open_wearables_retention_policy_binding,
    persist_open_wearables_query_snapshot,
)
from healthmes.wearables.whoop_recovery import (
    WHOOP_RECOVERY_PACKAGE_CAPABILITY,
    calculate_whoop_recovery_package,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
LOCAL_DAY = date(2026, 8, 16)
MODEL = "whoop-migration-e2e-model"
PROVIDER = "whoop-migration-e2e-provider"
FINGERPRINT_KEY = b"whoop-migration-e2e-fingerprint-key"
WHOOP_PROVIDER_BINDING_DIGEST = "sha256:" + "a" * 64


def _persist_whoop_snapshot(
    session: Session,
    *,
    recovery_value: float,
    day_strain_value: float,
    suffix: str,
    collected_at: datetime,
):
    start = datetime.combine(
        LOCAL_DAY,
        datetime.min.time(),
        tzinfo=UTC,
    )
    end = start + timedelta(days=1)
    cycle_id = f"cycle-{suffix}"
    calculation = calculate_whoop_recovery_package(
        (
            {
                "id": f"recovery-{suffix}",
                "provider": "whoop",
                "category": "recovery",
                "recorded_at": (
                    start + timedelta(hours=4)
                ).isoformat(),
                "value": recovery_value,
                "components": {
                    "cycle_id": {"qualifier": cycle_id},
                },
            },
        ),
        (
            {
                "id": f"strain-{suffix}",
                "provider": "whoop",
                "category": "day_strain",
                "recorded_at": (
                    start + timedelta(hours=5)
                ).isoformat(),
                "value": day_strain_value,
                "components": {
                    "cycle_id": {"qualifier": cycle_id},
                    "cycle_updated_at": {
                        "qualifier": (
                            start + timedelta(hours=5, minutes=30)
                        ).isoformat(),
                    },
                },
            },
        ),
        as_of=LOCAL_DAY,
        timezone="UTC",
    )
    result = dict(calculation.public)
    result["retention_window"] = (
        domain_providers._wearable_retention_window(
            start=start,
            end=end,
            effective_now=NOW,
            retention_policy=(
                open_wearables_retention_policy_binding(session)
            ),
        )
    )
    return persist_open_wearables_query_snapshot(
        session,
        capability=WHOOP_RECOVERY_PACKAGE_CAPABILITY,
        start=start,
        end=end,
        timezone="UTC",
        parameters={"as_of": LOCAL_DAY.isoformat()},
        result=result,
        private_provenance=calculation.provenance,
        collected_at=collected_at,
        now=NOW,
        provider_binding_digest=WHOOP_PROVIDER_BINDING_DIGEST,
    )


class _WhoopMigrationHermesTransport:
    def __init__(
        self,
        *,
        search_service: DecisionContextSearchSessionService,
        expected_snapshot_id: uuid.UUID,
    ) -> None:
        self._search_service = search_service
        self._expected_snapshot_id = expected_snapshot_id
        self._rejected_latest_snapshot_id: uuid.UUID | None = None
        self.create_calls = 0
        self.runtime_verifications = 0
        self.deleted_sessions: list[str] = []
        self.runtime_questions: list[str] = []
        self.aliases: list[str] = []
        self.search_results = []
        self.last_error: BaseException | None = None

    def reject_newer_snapshot(self, snapshot_id: uuid.UUID) -> None:
        self._rejected_latest_snapshot_id = snapshot_id

    async def verify_runtime(self, *, timeout_seconds: float) -> None:
        assert timeout_seconds > 0
        self.runtime_verifications += 1

    async def get_toolsets(self) -> Mapping[str, Any]:
        return {
            "object": "list",
            "platform": "api_server",
            "data": [],
        }

    async def get_models(self) -> Mapping[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": MODEL,
                    "object": "model",
                    "created": int(NOW.timestamp()),
                    "owned_by": "hermes",
                    "permission": [],
                    "root": MODEL,
                    "parent": "healthmes-decision-runtime",
                },
            ],
        }

    async def create_response(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> HermesResponsesHttpResult:
        try:
            return await self._create_response(
                payload,
                timeout_seconds=timeout_seconds,
            )
        except BaseException as exc:
            self.last_error = exc
            raise

    async def _create_response(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> HermesResponsesHttpResult:
        assert timeout_seconds > 0
        await self.verify_runtime(timeout_seconds=timeout_seconds)
        self.create_calls += 1

        input_item = payload["input"][0]
        request_payload = json.loads(str(input_item["content"]))
        runtime_question = str(request_payload["question"])
        self.runtime_questions.append(runtime_question)
        assert str(self._expected_snapshot_id) not in runtime_question
        if self._rejected_latest_snapshot_id is not None:
            assert (
                str(self._rejected_latest_snapshot_id)
                not in runtime_question
            )

        related_records = request_payload["hints"]["related_records"]
        follow_up = self.create_calls == 2
        if follow_up:
            assert request_payload["hints"]["has_related_records"] is True
            assert len(related_records) == 1
            alias = str(related_records[0]["reference"])
            assert re.fullmatch(r"rr_[0-9a-f]{16}", alias)
            assert related_records[0]["domain"] == "wearable"
            assert related_records[0]["hint_keys"] == [
                "whoop_recovery_package"
            ]
            self.aliases.append(alias)
        else:
            assert request_payload["hints"]["has_related_records"] is False
            assert related_records == []
            alias = None

        decision_session_id = str(
            request_payload["decision_session_id"]
        )
        search_parameters = {"date": LOCAL_DAY.isoformat()}
        if alias is not None:
            search_parameters["package_record_id"] = alias
        result = await self._search_service.search(
            decision_session_id,
            domain="wearable",
            capability=WHOOP_RECOVERY_PACKAGE_CAPABILITY,
            granularity="day",
            parameters=search_parameters,
        )
        self.search_results.append(result)
        assert result.status.value in {"ok", "partial"}
        assert result.payload["limitations"] == []
        assert "provenance_mode" not in result.payload
        assert result.payload["recovery"]["label"] == "green"
        assert result.payload["day_strain"]["label"] == "moderate"
        assert result.payload["level"] == "basic"
        assert [ref.record_id for ref in result.source_refs] == [
            str(self._expected_snapshot_id)
        ]
        if self._rejected_latest_snapshot_id is not None:
            assert str(self._rejected_latest_snapshot_id) not in {
                ref.record_id for ref in result.source_refs
            }

        selected_actions = []
        for raw_action in result.payload["actions"]:
            action = dict(raw_action)
            if (
                follow_up
                and action["kind"] == "walk"
                and action["duration_minutes"] == 20
            ):
                action["state"] = "selected"
            selected_actions.append(action)

        call_id = f"call-whoop-migration-e2e-{self.create_calls}"
        tool_arguments = {
            "decision_session_id": decision_session_id,
            "capability": WHOOP_RECOVERY_PACKAGE_CAPABILITY,
            "granularity": "day",
            "date": LOCAL_DAY.isoformat(),
        }
        if alias is not None:
            tool_arguments["package_record_id"] = alias
        answer = (
            "Using the same current-day, high-confidence WHOOP snapshot "
            "with no material data limitations, you selected the "
            "20-minute easy walk from the offered 10, 20, and 30-minute "
            "choices. Keep the water and 30-minute-earlier bedtime "
            "preparation recommendations."
            if follow_up
            else (
                "Your current-day WHOOP Recovery is green and day strain "
                "is moderate. Package confidence is high, with no "
                "material data limitations. Start with a 10-minute easy "
                "walk, or choose 20 or 30 minutes; drink water and begin "
                "bedtime preparation 30 minutes earlier."
            )
        )
        envelope = {
            "schema": "healthmes.decision-draft.v2",
            "decision": {
                "status": "completed",
                "answer": answer,
                "record_summary": None,
                "record_summary_code": "take_restorative_break",
                "proposed_action": True,
                "actions": selected_actions,
                "persistence_intent": "action",
                "used_source_ref_ids": [
                    result.source_refs[0].reference_id
                ],
                "limitations": list(result.limitations),
                "clarification_question": None,
                "confidence": 0.9,
                "uncertainty": (
                    "Only the retained WHOOP package was considered."
                ),
                "follow_up_question": None,
            },
        }
        response = {
            "id": "resp-whoop-migration-e2e",
            "object": "response",
            "status": "completed",
            "created_at": int(NOW.timestamp()),
            "model": MODEL,
            "output": [
                {
                    "type": "function_call",
                    "name": "mcp__healthmes__search_wearable",
                    "arguments": json.dumps(tool_arguments),
                    "call_id": call_id,
                },
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(
                        {
                            "structuredContent": result.model_dump(
                                mode="json",
                                round_trip=True,
                            )
                        }
                    ),
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(envelope),
                        }
                    ],
                },
            ],
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
            },
        }
        return HermesResponsesHttpResult(
            payload=response,
            session_id=(
                f"hermes-whoop-migration-e2e-{self.create_calls}"
            ),
        )

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        return {
            "object": "list",
            "data": [],
            "limit": limit,
            "offset": offset,
            "has_more": False,
        }

    async def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)


async def test_exact_prior_whoop_package_survives_official_runtime_and_replay(
    tmp_path,
    settings,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{tmp_path / 'whoop-migration-e2e.db'}"
    )
    db_engine = create_db_engine(database_url)
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)

    try:
        with factory() as session:
            ensure_decision_domain_policies(session, "owner")
            first = _persist_whoop_snapshot(
                session,
                recovery_value=70,
                day_strain_value=12,
                suffix="first",
                collected_at=NOW - timedelta(hours=2),
            )
            session.commit()

        upstream_calls = 0

        async def forbidden_upstream_reader(_request):
            nonlocal upstream_calls
            upstream_calls += 1
            raise AssertionError(
                "the initial lookup must use the retained fallback"
            )

        provider = WearableContextProvider(
            search_reader=forbidden_upstream_reader,
            snapshot_session_factory=factory,
            clock=lambda: NOW,
        )
        access_layer = ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=lambda: NOW,
        )
        policy_resolver = DatabaseDecisionPolicyResolver(
            session_factory=factory,
            owner_principal_id="owner",
            execution_scope=ExecutionScope.LOCAL,
        )

        async def whoop_availability() -> OpenWearablesAvailabilitySnapshot:
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.AVAILABLE,
                observed_at=NOW,
                provider_catalog_version=1,
                providers=("whoop",),
                provider_bindings=(
                    OpenWearablesProviderBinding(
                        provider="whoop",
                        direct_api=True,
                        capabilities=(
                            WHOOP_RECOVERY_PACKAGE_CAPABILITY,
                        ),
                    ),
                ),
                capability_catalog=(
                    OpenWearablesCapabilityBinding(
                        capability=WHOOP_RECOVERY_PACKAGE_CAPABILITY,
                        providers=("whoop",),
                    ),
                ),
                provider_source_bindings=(
                    OpenWearablesProviderSourceBinding(
                        provider="whoop",
                        active_connection_ids=("whoop-connection",),
                        direct_data_source_ids=("whoop-data-source",),
                    ),
                ),
                provider_binding_digest=(
                    WHOOP_PROVIDER_BINDING_DIGEST
                ),
            )

        search_service = DecisionContextSearchSessionService(
            access_layer=access_layer,
            session_factory=factory,
            policy_resolver=policy_resolver,
            clock=lambda: NOW,
            open_wearables_availability=whoop_availability,
        )
        transport = _WhoopMigrationHermesTransport(
            search_service=search_service,
            expected_snapshot_id=first.event_id,
        )
        engine = build_healthmes_responses_decision_engine(
            transport=transport,
            search_service=search_service,
            session_factory=factory,
            policy_resolver=policy_resolver,
            fingerprint_key=FINGERPRINT_KEY,
            model=MODEL,
            provider=PROVIDER,
            timeout_seconds=5,
            cleanup_timeout_seconds=1,
            finalization_timeout_seconds=5,
            clock=lambda: NOW,
        )
        service_settings = settings.model_copy(
            update={
                "database_url": database_url,
                "decision_owner_principal_id": "owner",
                "timezone": "UTC",
            }
        )
        service = HealthMesDecisionService(
            settings=service_settings,
            engine_provider=lambda: engine,
            session_factory_provider=lambda: factory,
            clock=lambda: NOW,
        )
        channel = DecisionChannelAdapter(service=service)
        first_submission = DecisionChannelRequest(
            idempotency_key="whoop-first-recommendation",
            question="How should I recover today based on WHOOP?",
            source="ios-whoop-e2e",
            requested_at=NOW,
        )

        async with engine:
            first_result = await channel.ask_wellness(first_submission)
            assert first_result.status is DecisionStatus.COMPLETED, {
                "result": first_result.model_dump(
                    mode="json",
                    round_trip=True,
                ),
                "transport_error": repr(transport.last_error),
            }
            assert (
                first_result.persistence_status
                is PersistenceStatus.PERSISTED
            )
            assert first_result.decision_record_id is not None
            assert first_result.answer is not None
            assert "Recovery is green" in first_result.answer
            assert "day strain is moderate" in first_result.answer
            assert "current-day" in first_result.answer
            assert "confidence is high" in first_result.answer
            assert "no material data limitations" in first_result.answer
            assert "choose 20 or 30 minutes" in first_result.answer
            assert first_result.answer != decision_record_summary(
                DecisionRecordSummaryCode.TAKE_RESTORATIVE_BREAK
            )
            assert [ref.record_id for ref in first_result.source_refs] == [
                str(first.event_id)
            ]
            assert first_result.related_record_ids == {
                "whoop_recovery_package": str(first.event_id)
            }
            assert not any(
                action.state is DecisionActionState.SELECTED
                for action in first_result.actions
            )

            with factory() as session:
                latest = _persist_whoop_snapshot(
                    session,
                    recovery_value=45,
                    day_strain_value=16,
                    suffix="latest",
                    collected_at=NOW - timedelta(hours=1),
                )
                session.commit()
            assert first.event_id != latest.event_id
            transport.reject_newer_snapshot(latest.event_id)

            follow_up_submission = DecisionChannelRequest(
                idempotency_key="whoop-select-20-minute-walk",
                question=(
                    "Use the same WHOOP recovery recommendation and select "
                    "the 20-minute walk."
                ),
                source="ios-whoop-e2e",
                requested_at=NOW,
                hints=DecisionContextHints(
                    related_record_ids=first_result.related_record_ids
                ),
            )
            follow_up = await channel.ask_wellness(
                follow_up_submission
            )

            assert follow_up.status is DecisionStatus.COMPLETED, {
                "result": follow_up.model_dump(
                    mode="json",
                    round_trip=True,
                ),
                "transport_error": repr(transport.last_error),
                "runtime_questions": transport.runtime_questions,
            }
            assert (
                follow_up.persistence_status
                is PersistenceStatus.PERSISTED
            )
            assert follow_up.decision_record_id is not None
            assert follow_up.decision_record_id != (
                first_result.decision_record_id
            )
            assert follow_up.answer is not None
            assert "selected the 20-minute easy walk" in follow_up.answer
            assert "current-day, high-confidence" in follow_up.answer
            assert "no material data limitations" in follow_up.answer
            assert "10, 20, and 30-minute choices" in follow_up.answer
            assert [ref.record_id for ref in follow_up.source_refs] == [
                str(first.event_id)
            ]
            assert follow_up.related_record_ids == {
                "whoop_recovery_package": str(first.event_id)
            }
            assert str(latest.event_id) not in {
                ref.record_id for ref in follow_up.source_refs
            }
            selected = [
                action
                for action in follow_up.actions
                if action.state is DecisionActionState.SELECTED
            ]
            assert len(selected) == 1
            assert selected[0].kind is DecisionActionKind.WALK
            assert selected[0].duration_minutes == 20
            assert follow_up.tool_trace[0].query.parameters[
                "package_record_id"
            ] == transport.aliases[0]
            assert follow_up.tool_trace[0].effective_query is not None
            assert follow_up.tool_trace[0].effective_query.parameters[
                "package_record_id"
            ] == str(first.event_id)

            restarted_service = HealthMesDecisionService(
                settings=service_settings,
                engine_provider=lambda: engine,
                session_factory_provider=lambda: factory,
                clock=lambda: NOW,
            )
            restarted_channel = DecisionChannelAdapter(
                service=restarted_service
            )
            first_replay = await restarted_channel.ask_wellness(
                first_submission
            )
            follow_up_replay = await restarted_channel.ask_wellness(
                follow_up_submission
            )
            await restarted_service.aclose()
            await service.aclose()

        assert first_replay.status is DecisionStatus.COMPLETED
        assert (
            first_replay.persistence_status
            is PersistenceStatus.PERSISTED
        )
        assert first_replay.decision_record_id == (
            first_result.decision_record_id
        )
        assert first_replay.source_refs == first_result.source_refs
        assert first_replay.related_record_ids == (
            first_result.related_record_ids
        )
        assert first_replay.actions == first_result.actions
        assert first_replay.answer == decision_record_summary(
            DecisionRecordSummaryCode.TAKE_RESTORATIVE_BREAK
        )
        assert first_replay.answer != first_result.answer
        assert "decision_response_compacted" in first_replay.limitations
        assert first_replay.tool_trace == []

        assert follow_up_replay.status is DecisionStatus.COMPLETED
        assert (
            follow_up_replay.persistence_status
            is PersistenceStatus.PERSISTED
        )
        assert follow_up_replay.decision_record_id == (
            follow_up.decision_record_id
        )
        assert follow_up_replay.source_refs == follow_up.source_refs
        assert follow_up_replay.related_record_ids == (
            follow_up.related_record_ids
        )
        assert follow_up_replay.actions == follow_up.actions
        assert follow_up_replay.answer == decision_record_summary(
            DecisionRecordSummaryCode.TAKE_RESTORATIVE_BREAK
        )
        assert follow_up_replay.answer != follow_up.answer
        assert "decision_response_compacted" in (
            follow_up_replay.limitations
        )
        assert follow_up_replay.tool_trace == []

        assert transport.create_calls == 2
        assert upstream_calls == 1
        assert transport.deleted_sessions == [
            "hermes-whoop-migration-e2e-1",
            "hermes-whoop-migration-e2e-2",
        ]

        with factory() as session:
            rows = tuple(session.scalars(select(DecisionRecord)))
            assert len(rows) == 2
            rows_by_id = {row.id: row for row in rows}
            first_row = rows_by_id[first_result.decision_record_id]
            follow_up_row = rows_by_id[follow_up.decision_record_id]
            for row in (first_row, follow_up_row):
                assert row.decision_payload is not None
                assert row.decision_payload["schema"] == (
                    "healthmes.decision-private.v7"
                )
                assert row.decision_payload["source_refs"][0][
                    "record_id"
                ] == str(first.event_id)
            first_stored_actions = first_row.decision_payload[
                "outcome"
            ]["actions"]
            assert not any(
                action["state"] == "selected"
                for action in first_stored_actions
            )
            stored_actions = follow_up_row.decision_payload["outcome"][
                "actions"
            ]
            assert any(
                action["kind"] == "walk"
                and action["state"] == "selected"
                and action["duration_minutes"] == 20
                for action in stored_actions
            )
            serialized_payload = json.dumps(
                [
                    first_row.decision_payload,
                    follow_up_row.decision_payload,
                ],
                sort_keys=True,
            )
            assert "recovery-first" not in serialized_payload
            assert "strain-first" not in serialized_payload
            assert "cycle-first" not in serialized_payload
            assert first_result.answer not in serialized_payload
            assert follow_up.answer not in serialized_payload

            first_event = session.get(WellnessEvent, first.event_id)
            latest_event = session.get(WellnessEvent, latest.event_id)
            assert first_event is not None
            assert latest_event is not None
            assert first_event.payload["private_provenance"]
            assert latest_event.payload["private_provenance"]
    finally:
        db_engine.dispose()
