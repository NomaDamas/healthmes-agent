import datetime as dt
import threading
import uuid

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

import healthmes.nutrition.intake_service as intake_service_module
from healthmes.mcp_server import caffeine_adapter
from healthmes.mcp_server import server as server_module
from healthmes.nutrition.contracts import (
    CaffeineConfirmation,
    CaptureContext,
    Confidence,
    ConfirmationStatus,
    ConfirmedCaffeineItem,
    DailyIntakeConfirmation,
    Estimate,
    EstimateKind,
    IntakeItem,
    IntakeType,
    MetadataSource,
    NutritionObservation,
    ObservationStatus,
    VisionProvenance,
)
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    EvidenceOrigin,
    IntakeIntent,
    IntakeInteraction,
    IntakeInteractionReview,
    IntakeOutcome,
    IntakeOutcomeStatus,
    IntakeReviewStatus,
    NormalizedIntakeItem,
    NutrientFact,
)
from healthmes.nutrition.intake_service import (
    create_interaction,
    create_photo_interaction,
    operation_fingerprint,
    persist_interaction_review,
    persist_outcome,
)
from healthmes.nutrition.operation_integrity import result_payload_digest
from healthmes.nutrition.repository import (
    persist_caffeine_confirmation,
    persist_daily_confirmation,
    persist_observation,
)
from healthmes.storage import register_storage_object
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    WellnessEvent,
)
from healthmes.trusted_session import issue_trusted_session_proof

OWNER_PROOF_SECRET = "test-calendar-adjustment-secret-32-characters"


class _AutoConfirmNutritionArguments(dict):
    _auto_confirm_nutrition = True


def _trusted(tool_name, arguments):
    if tool_name in {
        "confirm_intake_outcome",
        "review_intake_interaction",
    }:
        return _AutoConfirmNutritionArguments(arguments)
    return dict(arguments)


def _trusted_caffeine(arguments):
    return _AutoConfirmNutritionArguments(
        {
            "operation_id": str(uuid.uuid4()),
            **arguments,
        }
    )


def _seed_event(
    store_factory,
    *,
    start: dt.datetime,
    end: dt.datetime,
    summary: str = "Focused work",
) -> uuid.UUID:
    with store_factory() as session:
        event = CalendarEventMirror(
            external_id=f"caffeine-{uuid.uuid4()}",
            calendar_source=CalendarSource.GOOGLE,
            summary=summary,
            start_at=start,
            end_at=end,
        )
        session.add(event)
        session.flush()
        event_id = event.id
        session.commit()
    return event_id


def _proposal_args(
    event_id: uuid.UUID,
    *,
    event_start_local: dt.datetime,
    target_sleep_local: dt.datetime,
) -> dict[str, object]:
    baseline_confirmed_at = min(
        server_module._utc_now().astimezone(event_start_local.tzinfo) - dt.timedelta(minutes=1),
        event_start_local - dt.timedelta(hours=1),
    )
    return {
        "event_id": str(event_id),
        "personal_daily_limit_mg": 300,
        "population_status": "confirmed_adult",
        "product_form": "beverage_or_food",
        "intended_consumption_at": event_start_local.isoformat(),
        "target_sleep_at": target_sleep_local.isoformat(),
        "personal_event_baseline_mg": 100,
        "baseline_confirmed_at": baseline_confirmed_at.isoformat(),
        "cutoff_before_sleep_hours": 6,
        "contraindications": [],
    }


def _candidate_items(
    amount_mg: float,
    *,
    origin: str = "user",
) -> list[dict[str, object]]:
    return [
        {
            "name": "candidate coffee",
            "intake_type": "beverage",
            "serving": {
                "kind": "exact",
                "unit": "serving",
                "exact": 1,
                "estimation_basis": "owner_statement",
            },
            "nutrients": [
                {
                    "nutrient": "caffeine",
                    "amount": {
                        "kind": "exact",
                        "unit": "mg",
                        "exact": amount_mg,
                        "estimation_basis": "owner_statement",
                    },
                    "confidence": "high",
                    "origin": origin,
                }
            ],
            "confidence": "high",
            "warnings": [],
        }
    ]


def test_candidate_context_rejects_non_finite_aggregate() -> None:
    context = {
        "status": "ok",
        "request": {"scope": "caffeine_sleep"},
        "candidate": {
            "interaction_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "is_confirmed_intake": False,
            "latest_review": {"status": "confirmed"},
            "resolved_items": [
                *_candidate_items(1e308),
                *_candidate_items(1e308),
            ],
        },
    }

    candidate, reason = caffeine_adapter.candidate_caffeine_from_context(
        context,
        decision_request_id=str(uuid.uuid4()),
    )

    assert candidate is None
    assert reason == "candidate_caffeine_total_invalid"


@pytest.mark.parametrize("origin", ["user", "label"])
def test_candidate_context_requires_owner_review_for_direct_exact_mg(
    origin: str,
) -> None:
    context = {
        "status": "ok",
        "request": {"scope": "caffeine_sleep"},
        "candidate": {
            "interaction_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "is_confirmed_intake": False,
            "latest_review": None,
            "resolved_items": _candidate_items(120, origin=origin),
        },
    }

    candidate, reason = caffeine_adapter.candidate_caffeine_from_context(
        context,
        decision_request_id=str(uuid.uuid4()),
    )

    assert candidate is None
    assert reason == "candidate_nutrition_requires_owner_review"


def test_candidate_context_requires_exact_caffeine_for_every_item() -> None:
    unquantified_item = _candidate_items(0)[0]
    unquantified_item["name"] = "energy bar"
    unquantified_item["nutrients"] = []
    context = {
        "status": "ok",
        "request": {"scope": "caffeine_sleep"},
        "candidate": {
            "interaction_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "is_confirmed_intake": False,
            "latest_review": {"status": "confirmed"},
            "resolved_items": [
                *_candidate_items(100),
                unquantified_item,
            ],
        },
    }

    candidate, reason = caffeine_adapter.candidate_caffeine_from_context(
        context,
        decision_request_id=str(uuid.uuid4()),
    )

    assert candidate is None
    assert reason == ("candidate_caffeine_requires_exact_user_or_label_mg")


def _seed_confirmed_caffeine(
    store_factory,
    *,
    day: dt.date,
    amount_mg: float = 100,
    confirm: bool = True,
) -> uuid.UUID:
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    local_timezone = dt.timezone(dt.timedelta(hours=9))
    local_now = dt.datetime.now(local_timezone)
    observed_at = (
        local_now.astimezone(dt.UTC)
        if day == local_now.date()
        else dt.datetime.combine(
            day,
            dt.time(9),
            tzinfo=local_timezone,
        ).astimezone(dt.UTC)
    )
    media_path = f"media/caffeine/{uuid.uuid4()}.jpg"
    target = settings.data_dir / media_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fixture")
    observation = NutritionObservation(
        observation_id=uuid.uuid4(),
        capture=CaptureContext(
            media_path=media_path,
            captured_at=observed_at,
            timezone="Asia/Seoul",
            source="caffeine-tool-test",
            location=None,
            metadata_provenance={
                "captured_at": MetadataSource.FIXTURE,
                "timezone": MetadataSource.FIXTURE,
                "location": MetadataSource.UNAVAILABLE,
            },
        ),
        status=ObservationStatus.USABLE,
        confidence=Confidence.HIGH,
        warnings=(),
        items=(
            IntakeItem(
                intake_type=IntakeType.BEVERAGE,
                name_candidates=("coffee",),
                category="coffee",
                serving=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="ml",
                    exact=250,
                    estimation_basis="fixture",
                ),
                caffeine=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="mg",
                    exact=amount_mg,
                    estimation_basis="fixture",
                ),
                confidence=Confidence.HIGH,
            ),
        ),
        vision=VisionProvenance(
            provider="fixture",
            model="fixture-v1",
            model_digest="sha256:fixture",
            prompt_version="fixture-v1",
            schema_version="nutrition-observation-v1",
            analyzed_at=observed_at + dt.timedelta(seconds=1),
        ),
    )
    with store_factory() as session:
        register_storage_object(
            session,
            settings,
            relative_path=media_path,
            data_class="media",
            content_type="image/jpeg",
            size_bytes=7,
            observed_at=observed_at,
        )
        persist_observation(
            session,
            settings,
            observation,
            request_fingerprint=str(observation.observation_id),
        )
        # Observation capture and user confirmation are separate API writes.
        session.commit()
        if confirm:
            persist_caffeine_confirmation(
                session,
                CaffeineConfirmation(
                    confirmation_id=uuid.uuid4(),
                    observation_id=observation.observation_id,
                    status=ConfirmationStatus.CONFIRMED,
                    confirmed_at=observed_at + dt.timedelta(minutes=1),
                    source="fixture-user",
                    items=(
                        ConfirmedCaffeineItem(
                            item_index=0,
                            caffeine_mg=amount_mg,
                        ),
                    ),
                ),
                operation_fingerprint="e" * 64,
            )
            persist_daily_confirmation(
                session,
                DailyIntakeConfirmation(
                    confirmation_id=uuid.uuid4(),
                    local_date=day,
                    timezone="Asia/Seoul",
                    observation_ids=(observation.observation_id,),
                    total_intake_complete=True,
                    confirmed_at=observed_at + dt.timedelta(minutes=2),
                    source="fixture-user",
                ),
                operation_fingerprint="c" * 64,
            )
        session.commit()
    return observation.observation_id


def _seed_confirmed_text_caffeine(
    store_factory,
    *,
    day: dt.date,
    amount_mg: float,
    origin: EvidenceOrigin = EvidenceOrigin.USER,
    confirm_day: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    local_timezone = dt.timezone(dt.timedelta(hours=9))
    local_now = dt.datetime.now(local_timezone)
    observed_at = (
        local_now.astimezone(dt.UTC)
        if day == local_now.date()
        else dt.datetime.combine(
            day,
            dt.time(9),
            tzinfo=local_timezone,
        ).astimezone(dt.UTC)
    )
    interaction_id = uuid.uuid4()
    interaction = IntakeInteraction(
        interaction_id=interaction_id,
        operation_fingerprint="a" * 64,
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=observed_at,
        recorded_at=observed_at,
        timezone="Asia/Seoul",
        source="caffeine-tool-test",
        source_text=f"coffee {amount_mg} mg caffeine",
        media_path=None,
        nutrition_observation_id=None,
        items=(
            NormalizedIntakeItem(
                name="coffee",
                intake_type="beverage",
                serving=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="cup",
                    exact=1,
                    estimation_basis="owner_statement",
                ),
                nutrients=(
                    NutrientFact(
                        nutrient="caffeine",
                        amount=Estimate(
                            kind=EstimateKind.EXACT,
                            unit="mg",
                            exact=amount_mg,
                            estimation_basis="owner_statement",
                        ),
                        confidence=Confidence.HIGH,
                        origin=origin,
                    ),
                ),
                confidence=Confidence.HIGH,
            ),
        ),
    )
    outcome_id = uuid.uuid4()
    with store_factory() as session:
        create_interaction(session, settings, interaction)
        # Capture and consumption confirmation are separate API writes.
        session.commit()
        if origin in {EvidenceOrigin.USER, EvidenceOrigin.LABEL}:
            review_id = uuid.uuid4()
            persist_interaction_review(
                session,
                IntakeInteractionReview(
                    review_id=review_id,
                    operation_fingerprint=operation_fingerprint({"review_id": str(review_id)}),
                    interaction_id=interaction_id,
                    status=IntakeReviewStatus.CONFIRMED,
                    reviewed_at=observed_at + dt.timedelta(seconds=30),
                    source="fixture-user",
                ),
            )
            session.commit()
        persist_outcome(
            session,
            IntakeOutcome(
                outcome_id=outcome_id,
                operation_fingerprint="b" * 64,
                interaction_id=interaction_id,
                status=IntakeOutcomeStatus.CONSUMED,
                confirmed_at=observed_at + dt.timedelta(minutes=1),
                source="fixture-user",
                consumed_at=observed_at,
            ),
        )
        if confirm_day:
            persist_daily_confirmation(
                session,
                DailyIntakeConfirmation(
                    confirmation_id=uuid.uuid4(),
                    local_date=day,
                    timezone="Asia/Seoul",
                    observation_ids=(),
                    total_intake_complete=True,
                    confirmed_at=observed_at + dt.timedelta(minutes=2),
                    source="fixture-user",
                    outcome_ids=(outcome_id,),
                ),
                operation_fingerprint="d" * 64,
            )
        session.commit()
    return interaction_id, outcome_id


def _local_times(
    pinned_tz: dt.tzinfo,
    *,
    days_from_today: int = 0,
) -> tuple[dt.date, dt.datetime, dt.datetime]:
    day = dt.datetime.now(pinned_tz).date() + dt.timedelta(days=days_from_today)
    event_start = dt.datetime.combine(day, dt.time(13), tzinfo=pinned_tz)
    target_sleep = dt.datetime.combine(day, dt.time(23), tzinfo=pinned_tz)
    return day, event_start, target_sleep


async def _seed_candidate_request(
    mcp_client,
    call_tool,
    *,
    event_start: dt.datetime,
    amount_mg: float,
) -> tuple[str, dict[str, object]]:
    capture_arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "ask_before_intake",
        "modality": "text",
        "source_text": f"카페인 {amount_mg}mg 커피를 마셔도 될까?",
        "observed_at": None,
        "media_path": None,
        "nutrition_observation_id": None,
        "items": _candidate_items(amount_mg),
    }
    captured = await call_tool(
        mcp_client,
        "capture_intake_interaction",
        _trusted("capture_intake_interaction", capture_arguments),
    )
    interaction_id = captured["interaction"]["interaction_id"]
    await call_tool(
        mcp_client,
        "review_intake_interaction",
        _trusted(
            "review_intake_interaction",
            {
                "operation_id": str(uuid.uuid4()),
                "interaction_id": interaction_id,
                "status": "confirmed",
                "corrected_items": [],
            },
        ),
    )
    request_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "scope": "caffeine_sleep",
        "question": "지금 마셔도 될까?",
        "intended_consumption_at": event_start.isoformat(),
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }
    context = await call_tool(
        mcp_client,
        "request_intake_decision",
        _trusted("request_intake_decision", request_arguments),
    )
    return interaction_id, context


class TestCaffeineProposalTool:
    @pytest.fixture(autouse=True)
    def _use_iana_timezone(
        self,
        mcp_env,
        pinned_tz,
        monkeypatch,
    ):
        server_module.set_timezone("Asia/Seoul")
        fixed_local_now = dt.datetime.combine(
            dt.datetime.now(pinned_tz).date(),
            dt.time(10),
            tzinfo=pinned_tz,
        )
        monkeypatch.setattr(
            server_module,
            "_utc_now",
            lambda: fixed_local_now.astimezone(dt.UTC),
        )

    async def test_current_evidence_returns_personal_baseline_proposal_without_writes(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        observation_id = _seed_confirmed_caffeine(
            store_factory,
            day=day,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                _proposal_args(
                    event_id,
                    event_start_local=event_start,
                    target_sleep_local=target_sleep,
                )
            ),
        )

        assert result["status"] == "proposal"
        assert result["facts"]["target_event"]["id"] == str(event_id)
        assert result["facts"]["sleep"] == {
            "local_date": day.isoformat(),
            "duration_minutes": 374,
            "provider": "oura",
            "source_key": f"sleep-summary:oura:{day.isoformat()}",
            "freshness": "current",
        }
        assert result["facts"]["personal_daily_limit"] == {
            "amount_mg": 300,
            "source": "user_confirmed_via_agent",
        }
        assert result["facts"]["remaining_daily_allowance_mg"] == 200
        assert result["facts"]["caffeine_intake"]["status"] == "known"
        assert result["facts"]["caffeine_intake"]["evidence"][0]["observation_id"] == str(
            observation_id
        )
        assert result["recommendation"] == {
            "maximum_additional_mg": 200,
            "suggested_additional_mg": 100,
            "basis": "personal_event_baseline",
        }
        assert result["confidence"] == "medium"
        assert result["reason"] == "personal_event_baseline_applied"
        assert result["framing"] == "bounded_preparation_proposal_not_medical_advice"

        with store_factory() as session:
            events = list(session.scalars(select(CalendarEventMirror)))
        assert [(event.id, event.summary) for event in events] == [(event_id, "Focused work")]

    async def test_text_intake_is_included_and_decimal_mg_rounds_up(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        interaction_id, _ = _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=150.1,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                _proposal_args(
                    event_id,
                    event_start_local=event_start,
                    target_sleep_local=target_sleep,
                )
            ),
        )

        assert result["status"] == "proposal"
        assert result["facts"]["consumed_today_mg"] == 151
        assert result["facts"]["remaining_daily_allowance_mg"] == 149
        assert result["facts"]["caffeine_intake"]["confirmed_caffeine_mg"] == 150.1
        assert result["facts"]["caffeine_intake"]["consumed_outcome_count"] == 1
        assert result["facts"]["caffeine_intake"]["evidence"][0]["interaction_id"] == str(
            interaction_id
        )

    async def test_confirmed_candidate_is_combined_with_stored_daily_total(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=100,
        )
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "카페인 120mg 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(120),
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )
        await call_tool(
            mcp_client,
            "review_intake_interaction",
            _trusted(
                "review_intake_interaction",
                {
                    "operation_id": str(uuid.uuid4()),
                    "interaction_id": captured["interaction"]["interaction_id"],
                    "status": "confirmed",
                    "corrected_items": [],
                },
            ),
        )
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": captured["interaction"]["interaction_id"],
            "scope": "caffeine_sleep",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": event_start.isoformat(),
            "compare_interaction_ids": [],
            "lookback_days": 14,
        }
        context = await call_tool(
            mcp_client,
            "request_intake_decision",
            _trusted("request_intake_decision", request_arguments),
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                {
                    "event_id": None,
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "target_sleep_at": target_sleep.isoformat(),
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "intake_decision_request_id": context["request"]["request_id"],
                }
            ),
        )

        assert result["status"] == "proposal"
        assert result["reason"] == "candidate_within_bounded_limit"
        assert result["facts"]["target_event"] is None
        assert result["facts"]["consumed_today_mg"] == 100
        assert result["facts"]["candidate_caffeine"]["amount_mg"] == 120
        assert result["facts"]["candidate_total_after_intake_mg"] == 220
        assert result["recommendation"] == {
            "maximum_additional_mg": 200,
            "suggested_additional_mg": 120,
            "basis": "confirmed_candidate",
            "candidate_assessment": "within_bounded_limit",
        }

    async def test_candidate_request_rejects_runtime_timezone_change(
        self,
        mcp_client,
        call_tool,
        pinned_tz,
    ):
        _day, event_start, target_sleep = _local_times(pinned_tz)
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "카페인 120mg 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(120),
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": captured["interaction"]["interaction_id"],
            "scope": "caffeine_sleep",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": event_start.isoformat(),
            "compare_interaction_ids": [],
            "lookback_days": 14,
        }
        context = await call_tool(
            mcp_client,
            "request_intake_decision",
            _trusted("request_intake_decision", request_arguments),
        )
        assert context["request"]["timezone"] == "Asia/Seoul"
        assert context["request"]["intended_consumption_at"].endswith("+09:00")

        server_module.set_timezone("America/Los_Angeles")
        with pytest.raises(
            ToolError,
            match="runtime timezone conflicts with the stored intake decision",
        ):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": None,
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "target_sleep_at": target_sleep.isoformat(),
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "intake_decision_request_id": context["request"]["request_id"],
                    }
                ),
            )

    async def test_candidate_request_rejects_past_consumption_time(
        self,
        mcp_client,
        call_tool,
        pinned_tz,
    ):
        _day, event_start, _target_sleep = _local_times(pinned_tz)
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "카페인 120mg 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(120),
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": captured["interaction"]["interaction_id"],
            "scope": "caffeine_sleep",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": (
                event_start - dt.timedelta(hours=3, minutes=10)
            ).isoformat(),
            "compare_interaction_ids": [],
            "lookback_days": 14,
        }

        with pytest.raises(
            ToolError,
            match="cannot be more than 5 minutes in the past",
        ):
            await call_tool(
                mcp_client,
                "request_intake_decision",
                _trusted("request_intake_decision", request_arguments),
            )

    async def test_proposal_rejects_past_consumption_before_provider_lookup(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["intended_consumption_at"] = (
            event_start - dt.timedelta(hours=3, minutes=10)
        ).isoformat()

        with pytest.raises(
            ToolError,
            match="cannot be more than 5 minutes in the past",
        ):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(args),
            )

        assert mcp_env.requests == []
        assert day == event_start.date()

    async def test_consumed_candidate_rejects_stale_decision_request(
        self,
        mcp_client,
        call_tool,
        store_factory,
        pinned_tz,
    ):
        _day, event_start, target_sleep = _local_times(pinned_tz)
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "카페인 120mg 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(120),
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )
        interaction_id = captured["interaction"]["interaction_id"]
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": interaction_id,
            "scope": "caffeine_sleep",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": event_start.isoformat(),
            "compare_interaction_ids": [],
            "lookback_days": 14,
        }
        context = await call_tool(
            mcp_client,
            "request_intake_decision",
            _trusted("request_intake_decision", request_arguments),
        )
        outcome_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": interaction_id,
            "status": "consumed",
            "consumed_at": dt.datetime.now(dt.UTC).isoformat(),
            "corrected_items": [],
            "note": None,
        }
        await call_tool(
            mcp_client,
            "confirm_intake_outcome",
            _trusted("confirm_intake_outcome", outcome_arguments),
        )
        with store_factory() as session:
            outcome_event = session.scalar(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.intake-outcome.v1",
                    WellnessEvent.payload["interaction_id"].as_string() == interaction_id,
                )
            )
            assert outcome_event is not None
            session.delete(outcome_event)
            session.commit()

        with pytest.raises(ToolError, match="candidate_is_already_consumed"):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": None,
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "target_sleep_at": target_sleep.isoformat(),
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "intake_decision_request_id": context["request"]["request_id"],
                    }
                ),
            )

    async def test_candidate_time_cannot_be_overridden_after_request(
        self,
        mcp_client,
        call_tool,
        pinned_tz,
    ):
        _day, event_start, target_sleep = _local_times(pinned_tz)
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "카페인 120mg 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(120),
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": captured["interaction"]["interaction_id"],
            "scope": "caffeine_sleep",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": event_start.isoformat(),
            "compare_interaction_ids": [],
            "lookback_days": 14,
        }
        context = await call_tool(
            mcp_client,
            "request_intake_decision",
            _trusted("request_intake_decision", request_arguments),
        )

        with pytest.raises(
            ToolError,
            match="conflicts with the stored intake decision request",
        ):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": None,
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "intended_consumption_at": (
                            event_start + dt.timedelta(hours=1)
                        ).isoformat(),
                        "target_sleep_at": target_sleep.isoformat(),
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "intake_decision_request_id": context["request"]["request_id"],
                    }
                ),
            )

    async def test_candidate_request_without_stored_time_cannot_be_repaired(
        self,
        mcp_client,
        call_tool,
        store_factory,
        pinned_tz,
    ):
        _day, event_start, target_sleep = _local_times(pinned_tz)
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "카페인 120mg 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(120),
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": captured["interaction"]["interaction_id"],
            "scope": "caffeine_sleep",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": event_start.isoformat(),
            "compare_interaction_ids": [],
            "lookback_days": 14,
        }
        context = await call_tool(
            mcp_client,
            "request_intake_decision",
            _trusted("request_intake_decision", request_arguments),
        )
        request_id = context["request"]["request_id"]
        with store_factory() as session:
            event = session.scalar(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.decision-request.v1",
                    WellnessEvent.source_record_id == request_id,
                )
            )
            assert event is not None
            payload = dict(event.payload)
            context_snapshot = dict(payload["context_snapshot"])
            request_snapshot = dict(context_snapshot["request"])
            request_snapshot["intended_consumption_at"] = None
            context_snapshot["request"] = request_snapshot
            payload["context_snapshot"] = context_snapshot
            event.payload = payload
            marker = session.scalar(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.operation.v1",
                    WellnessEvent.source_provider == "nutrition-operation",
                    WellnessEvent.source_record_id == f"intake-decision-request:{request_id}",
                )
            )
            assert marker is not None
            marker.payload = {
                **marker.payload,
                "result_payload_sha256": result_payload_digest(payload),
            }
            session.commit()

        with pytest.raises(
            ToolError,
            match="stored intake decision intended_consumption_at is unavailable",
        ):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": None,
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "intended_consumption_at": event_start.isoformat(),
                        "target_sleep_at": target_sleep.isoformat(),
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "intake_decision_request_id": request_id,
                    }
                ),
            )

    async def test_prospective_photo_candidate_does_not_pollute_daily_ledger(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(
            store_factory,
            day=day,
            amount_mg=100,
        )
        candidate_observation_id = _seed_confirmed_caffeine(
            store_factory,
            day=day,
            amount_mg=120,
            confirm=False,
        )
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "photo",
            "source_text": "이 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": str(candidate_observation_id),
            "items": [],
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )
        interaction_id = captured["interaction"]["interaction_id"]
        assert captured["interaction"]["items"][0]["nutrients"][0]["origin"] == "vlm"

        review_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": interaction_id,
            "status": "confirmed",
            "corrected_items": [],
        }
        await call_tool(
            mcp_client,
            "review_intake_interaction",
            _trusted("review_intake_interaction", review_arguments),
        )
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": interaction_id,
            "scope": "caffeine_sleep",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": event_start.isoformat(),
            "compare_interaction_ids": [],
            "lookback_days": 14,
        }
        context = await call_tool(
            mcp_client,
            "request_intake_decision",
            _trusted("request_intake_decision", request_arguments),
        )
        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                {
                    "event_id": None,
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "target_sleep_at": target_sleep.isoformat(),
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "intake_decision_request_id": context["request"]["request_id"],
                }
            ),
        )

        assert result["status"] == "proposal"
        assert result["facts"]["consumed_today_mg"] == 100
        assert result["facts"]["candidate_caffeine"]["amount_mg"] == 120
        assert result["facts"]["caffeine_intake"]["observation_count"] == 1
        assert result["facts"]["caffeine_intake"]["captured_observation_count"] == 2
        assert result["facts"]["caffeine_intake"]["interaction_owned_observation_ids"] == [
            str(candidate_observation_id)
        ]

    async def test_unconfirmed_candidate_fails_closed_until_reviewed(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=100,
        )
        capture_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "이 커피를 마셔도 될까?",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(220, origin="agent"),
        }
        captured = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", capture_arguments),
        )

        async def request_context() -> dict[str, object]:
            arguments = {
                "operation_id": str(uuid.uuid4()),
                "interaction_id": captured["interaction"]["interaction_id"],
                "scope": "caffeine_sleep",
                "question": "지금 마셔도 될까?",
                "intended_consumption_at": event_start.isoformat(),
                "compare_interaction_ids": [],
                "lookback_days": 14,
            }
            return await call_tool(
                mcp_client,
                "request_intake_decision",
                _trusted("request_intake_decision", arguments),
            )

        first_context = await request_context()
        proposal_arguments = {
            "event_id": None,
            "personal_daily_limit_mg": 300,
            "population_status": "confirmed_adult",
            "product_form": "beverage_or_food",
            "target_sleep_at": target_sleep.isoformat(),
            "cutoff_before_sleep_hours": 6,
            "contraindications": [],
            "intake_decision_request_id": first_context["request"]["request_id"],
        }
        unconfirmed = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(proposal_arguments),
        )
        assert unconfirmed["status"] == "insufficient_data"
        assert unconfirmed["reason"] == "missing_candidate_caffeine"
        assert unconfirmed["facts"]["candidate_adapter_reason"] == (
            "candidate_nutrition_requires_owner_review"
        )

        review_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": captured["interaction"]["interaction_id"],
            "status": "confirmed",
            "corrected_items": [],
        }
        await call_tool(
            mcp_client,
            "review_intake_interaction",
            _trusted("review_intake_interaction", review_arguments),
        )
        with pytest.raises(
            ToolError,
            match="candidate_nutrition_changed",
        ):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(proposal_arguments),
            )
        reviewed_context = await request_context()
        reviewed = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                {
                    **proposal_arguments,
                    "intake_decision_request_id": reviewed_context["request"]["request_id"],
                }
            ),
        )
        assert reviewed["status"] == "noop"
        assert reviewed["reason"] == "candidate_exceeds_bounded_limit"
        assert reviewed["facts"]["candidate_caffeine"]["amount_mg"] == 220
        assert reviewed["facts"]["candidate_total_after_intake_mg"] == 320
        assert reviewed["recommendation"]["candidate_assessment"] == ("exceeds_bounded_limit")

    async def test_outcome_committed_during_proposal_invalidates_request(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
        monkeypatch,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        candidate_id, context = await _seed_candidate_request(
            mcp_client,
            call_tool,
            event_start=event_start,
            amount_mg=80,
        )
        client = server_module.get_ow_client()
        collect_sleep_summaries = client.collect_sleep_summaries

        async def commit_outcome_then_collect(*args, **kwargs):
            with store_factory() as session:
                persist_outcome(
                    session,
                    IntakeOutcome(
                        outcome_id=uuid.uuid4(),
                        operation_fingerprint="f" * 64,
                        interaction_id=uuid.UUID(candidate_id),
                        status=IntakeOutcomeStatus.CONSUMED,
                        confirmed_at=dt.datetime.now(dt.UTC),
                        source="fixture-user",
                        consumed_at=dt.datetime.now(dt.UTC),
                    ),
                )
                session.commit()
            return await collect_sleep_summaries(*args, **kwargs)

        monkeypatch.setattr(
            client,
            "collect_sleep_summaries",
            commit_outcome_then_collect,
        )
        with pytest.raises(
            ToolError,
            match="candidate_is_already_consumed",
        ):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": None,
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "target_sleep_at": target_sleep.isoformat(),
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "intake_decision_request_id": context["request"]["request_id"],
                    }
                ),
            )

    async def test_daily_ledger_change_during_confirmation_invalidates_snapshot(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
        monkeypatch,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(
            store_factory,
            day=day,
            amount_mg=100,
        )
        _candidate_id, context = await _seed_candidate_request(
            mcp_client,
            call_tool,
            event_start=event_start,
            amount_mg=80,
        )
        serialize_proposal = caffeine_adapter.serialize_proposal
        changed = False

        def commit_intake_then_serialize(*args, **kwargs):
            nonlocal changed
            proposal = serialize_proposal(*args, **kwargs)
            if not changed:
                _seed_confirmed_text_caffeine(
                    store_factory,
                    day=day,
                    amount_mg=250,
                    confirm_day=False,
                )
                changed = True
            return proposal

        monkeypatch.setattr(
            caffeine_adapter,
            "serialize_proposal",
            commit_intake_then_serialize,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                {
                    "event_id": None,
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "target_sleep_at": target_sleep.isoformat(),
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "intake_decision_request_id": context["request"]["request_id"],
                }
            ),
        )

        assert changed is True
        assert result["status"] == "invalidated"
        assert result["reason"] == (
            "nutrition data changed after confirmation was prepared; prepare a new confirmation"
        )

    async def test_final_candidate_lock_serializes_outcome_writer(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
        monkeypatch,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=100,
        )
        candidate_id, context = await _seed_candidate_request(
            mcp_client,
            call_tool,
            event_start=event_start,
            amount_mg=80,
        )
        known_caffeine_for_day = server_module.known_caffeine_for_day
        writer_started = threading.Event()
        writer_finished = threading.Event()
        writer_errors: list[BaseException] = []
        writer: threading.Thread | None = None
        ledger_reads = 0

        def commit_outcome() -> None:
            writer_started.set()
            try:
                with store_factory() as session:
                    persist_outcome(
                        session,
                        IntakeOutcome(
                            outcome_id=uuid.uuid4(),
                            operation_fingerprint="a" * 64,
                            interaction_id=uuid.UUID(candidate_id),
                            status=IntakeOutcomeStatus.CONSUMED,
                            confirmed_at=dt.datetime.now(dt.UTC),
                            source="fixture-user",
                            consumed_at=dt.datetime.now(dt.UTC),
                        ),
                    )
                    session.commit()
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                writer_finished.set()

        def observe_ledger(*args, **kwargs):
            nonlocal ledger_reads, writer
            value = known_caffeine_for_day(*args, **kwargs)
            ledger_reads += 1
            if ledger_reads == 4:
                writer = threading.Thread(target=commit_outcome)
                writer.start()
                assert writer_started.wait(timeout=2)
                assert not writer_finished.wait(timeout=0.2)
            return value

        monkeypatch.setattr(
            server_module,
            "known_caffeine_for_day",
            observe_ledger,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                {
                    "event_id": None,
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "target_sleep_at": target_sleep.isoformat(),
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "intake_decision_request_id": context["request"]["request_id"],
                }
            ),
        )

        assert result["status"] == "proposal"
        assert writer is not None
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert writer_finished.is_set()
        assert writer_errors == []

    async def test_final_candidate_lock_covers_comparison_interactions(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        pinned_tz,
        monkeypatch,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        primary_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "compare_option",
            "modality": "text",
            "source_text": "첫 번째 커피는 카페인 80mg",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(80),
        }
        comparison_arguments = {
            **primary_arguments,
            "operation_id": str(uuid.uuid4()),
            "source_text": "두 번째 커피는 카페인 40mg",
            "items": _candidate_items(40),
        }
        primary = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", primary_arguments),
        )
        comparison = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", comparison_arguments),
        )
        primary_id = primary["interaction"]["interaction_id"]
        comparison_id = comparison["interaction"]["interaction_id"]
        request_arguments = {
            "operation_id": str(uuid.uuid4()),
            "interaction_id": primary_id,
            "scope": "caffeine_sleep",
            "question": "둘 중 첫 번째를 마셔도 될까?",
            "intended_consumption_at": event_start.isoformat(),
            "compare_interaction_ids": [comparison_id],
            "lookback_days": 14,
        }
        context = await call_tool(
            mcp_client,
            "request_intake_decision",
            _trusted("request_intake_decision", request_arguments),
        )
        runtime_lock = server_module.lock_interaction_transition_states
        locked_sets: list[set[uuid.UUID]] = []

        def observe_candidate_locks(session, interaction_ids):
            locked_sets.append(set(interaction_ids))
            runtime_lock(session, interaction_ids)

        monkeypatch.setattr(
            server_module,
            "lock_interaction_transition_states",
            observe_candidate_locks,
        )

        await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                {
                    "event_id": None,
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "target_sleep_at": target_sleep.isoformat(),
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "intake_decision_request_id": context["request"]["request_id"],
                }
            ),
        )

        expected = {uuid.UUID(primary_id), uuid.UUID(comparison_id)}
        assert len(locked_sets) >= 2
        assert all(locked == expected for locked in locked_sets)

    async def test_final_time_check_rejects_candidate_that_expires_while_running(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        pinned_tz,
        monkeypatch,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _candidate_id, context = await _seed_candidate_request(
            mcp_client,
            call_tool,
            event_start=event_start,
            amount_mg=80,
        )
        before = (event_start - dt.timedelta(minutes=1)).astimezone(dt.UTC)
        after = (event_start + dt.timedelta(minutes=6)).astimezone(dt.UTC)
        calls = 0

        def advancing_clock() -> dt.datetime:
            nonlocal calls
            calls += 1
            return before if calls <= 3 else after

        monkeypatch.setattr(server_module, "_utc_now", advancing_clock)

        with pytest.raises(
            ToolError,
            match="cannot be more than 5 minutes in the past",
        ):
            await call_tool(
                mcp_client,
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": None,
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "target_sleep_at": target_sleep.isoformat(),
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "intake_decision_request_id": context["request"]["request_id"],
                    }
                ),
            )

        assert calls >= 4

    async def test_final_ledger_lock_serializes_other_intake_writer(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
        monkeypatch,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=100,
        )
        _candidate_id, context = await _seed_candidate_request(
            mcp_client,
            call_tool,
            event_start=event_start,
            amount_mg=80,
        )
        other_arguments = {
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "text",
            "source_text": "카페인 40mg 차를 마실 수도 있어",
            "observed_at": None,
            "media_path": None,
            "nutrition_observation_id": None,
            "items": _candidate_items(40),
        }
        other = await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", other_arguments),
        )
        other_interaction_id = uuid.UUID(other["interaction"]["interaction_id"])
        known_caffeine_for_day = server_module.known_caffeine_for_day
        runtime_lock = intake_service_module.lock_nutrition_ledger
        writer_attempted = threading.Event()
        writer_finished = threading.Event()
        writer_errors: list[BaseException] = []
        writer: threading.Thread | None = None
        ledger_reads = 0

        def observe_runtime_lock(session):
            writer_attempted.set()
            runtime_lock(session)

        def commit_other_outcome() -> None:
            try:
                consumed_at = server_module._utc_now()
                with store_factory() as session:
                    persist_outcome(
                        session,
                        IntakeOutcome(
                            outcome_id=uuid.uuid4(),
                            operation_fingerprint="b" * 64,
                            interaction_id=other_interaction_id,
                            status=IntakeOutcomeStatus.CONSUMED,
                            confirmed_at=consumed_at,
                            source="fixture-user",
                            consumed_at=consumed_at,
                        ),
                    )
                    session.commit()
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                writer_finished.set()

        def observe_ledger(*args, **kwargs):
            nonlocal ledger_reads, writer
            value = known_caffeine_for_day(*args, **kwargs)
            ledger_reads += 1
            if ledger_reads == 4:
                writer = threading.Thread(target=commit_other_outcome)
                writer.start()
                assert writer_attempted.wait(timeout=2)
                assert not writer_finished.wait(timeout=0.2)
            return value

        monkeypatch.setattr(
            intake_service_module,
            "lock_nutrition_ledger",
            observe_runtime_lock,
        )
        monkeypatch.setattr(
            server_module,
            "known_caffeine_for_day",
            observe_ledger,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                {
                    "event_id": None,
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "target_sleep_at": target_sleep.isoformat(),
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "intake_decision_request_id": context["request"]["request_id"],
                }
            ),
        )

        assert result["status"] == "proposal"
        assert writer is not None
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert writer_finished.is_set()
        assert writer_errors == []

    async def test_agent_estimate_is_not_promoted_to_known_intake(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=125.5,
            origin=EvidenceOrigin.AGENT,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                _proposal_args(
                    event_id,
                    event_start_local=event_start,
                    target_sleep_local=target_sleep,
                )
            ),
        )

        assert result["status"] == "insufficient_data"
        assert result["reason"] == "missing_total_intake"
        assert result["facts"]["caffeine_intake"]["status"] == "incomplete"
        assert result["facts"]["caffeine_intake"]["unquantified_outcome_ids"]

    async def test_later_outcome_invalidates_prior_daily_confirmation(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        interaction_id, _ = _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=125,
        )
        with store_factory() as session:
            persist_outcome(
                session,
                IntakeOutcome(
                    outcome_id=uuid.uuid4(),
                    operation_fingerprint="c" * 64,
                    interaction_id=interaction_id,
                    status=IntakeOutcomeStatus.NOT_CONSUMED,
                    confirmed_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=3),
                    source="fixture-user",
                ),
            )
            session.commit()

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                _proposal_args(
                    event_id,
                    event_start_local=event_start,
                    target_sleep_local=target_sleep,
                )
            ),
        )

        assert result["status"] == "insufficient_data"
        assert result["facts"]["caffeine_intake"]["status"] == "incomplete"
        assert result["facts"]["caffeine_intake"]["confirmed_caffeine_mg"] == 0

    async def test_photo_consumed_on_later_day_is_not_counted_twice(
        self,
        mcp_client,
        call_tool,
        store_factory,
        pinned_tz,
    ):
        today, _, _ = _local_times(pinned_tz)
        captured_day = today - dt.timedelta(days=1)
        observation_id = _seed_confirmed_caffeine(
            store_factory,
            day=captured_day,
            amount_mg=100,
        )
        interaction_id = uuid.uuid4()
        outcome_id = uuid.uuid4()
        with store_factory() as session:
            create_photo_interaction(
                session,
                server_module._active_settings(),
                observation_id=observation_id,
                operation_id=interaction_id,
                operation_fingerprint="d" * 64,
                intent=IntakeIntent.LOG_CONSUMED,
                source="caffeine-tool-test",
                recorded_at=dt.datetime.now(dt.UTC),
            )
            session.commit()
            persist_outcome(
                session,
                IntakeOutcome(
                    outcome_id=outcome_id,
                    operation_fingerprint="e" * 64,
                    interaction_id=interaction_id,
                    status=IntakeOutcomeStatus.CONSUMED,
                    confirmed_at=dt.datetime.now(dt.UTC),
                    source="fixture-user",
                    consumed_at=dt.datetime.now(dt.UTC),
                ),
            )
            session.commit()

        captured = await call_tool(
            mcp_client,
            "get_known_caffeine_intake_for_day",
            {"date": captured_day.isoformat()},
        )
        consumed = await call_tool(
            mcp_client,
            "get_known_caffeine_intake_for_day",
            {"date": today.isoformat()},
        )

        assert captured["status"] == "incomplete"
        assert captured["confirmed_caffeine_mg"] == 0
        assert captured["outcome_state_count"] == 1
        assert consumed["status"] == "incomplete"
        assert consumed["confirmed_caffeine_mg"] == 0
        assert consumed["evidence"][0]["nutrition_observation_id"] == str(observation_id)
        assert consumed["evidence"][0]["outcome_id"] == str(outcome_id)

    async def test_missing_owner_proof_rejects_before_provider_lookup(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        _, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        arguments = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        staged = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            {
                "operation_id": str(uuid.uuid4()),
                **arguments,
            },
        )
        handle = staged["reply_handle"]

        with pytest.raises(ToolError, match="trusted_session_proof"):
            await mcp_client.call_tool(
                "resolve_nutrition_confirmation",
                {
                    "response": f"확인 {handle}",
                    "reply_handle": handle,
                },
            )

        assert mcp_env.requests == []

    async def test_owner_proof_rejects_tampered_reply_before_provider_lookup(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        _, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        arguments = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        staged = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            {
                "operation_id": str(uuid.uuid4()),
                **arguments,
            },
        )
        handle = staged["reply_handle"]
        signed_arguments = {
            "response": f"확인 {handle}",
            "reply_handle": handle,
        }
        proof = issue_trusted_session_proof(
            OWNER_PROOF_SECRET,
            tool_name="resolve_nutrition_confirmation",
            arguments=signed_arguments,
            platform="telegram",
            chat_id="owner-chat",
            user_id="owner-user",
            message_id=str(uuid.uuid4()),
        )

        with pytest.raises(ToolError, match="trusted_session_proof"):
            await mcp_client.call_tool(
                "resolve_nutrition_confirmation",
                {
                    "response": f"취소 {handle}",
                    "reply_handle": handle,
                    "trusted_session_proof": proof,
                },
            )

        assert mcp_env.requests == []

    async def test_unknown_event_fails_closed_without_provider_lookup(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        pinned_tz,
    ):
        _, event_start, target_sleep = _local_times(pinned_tz)
        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                _proposal_args(
                    uuid.uuid4(),
                    event_start_local=event_start,
                    target_sleep_local=target_sleep,
                )
            ),
        )

        assert result["status"] == "insufficient_data"
        assert result["reason"] == "missing_target_event"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert mcp_env.requests == []

    async def test_future_event_sleep_is_stale_for_today_proposal(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz, days_from_today=1)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(
                _proposal_args(
                    event_id,
                    event_start_local=event_start,
                    target_sleep_local=target_sleep,
                )
            ),
        )

        assert result["status"] == "insufficient_data"
        assert result["facts"]["sleep"]["freshness"] == "stale"
        assert result["reason"] == "stale_sleep"
        assert result["recommendation"]["maximum_additional_mg"] is None

    async def test_missing_sleep_and_incomplete_intake_do_not_invent_numbers(
        self,
        mcp_client,
        call_tool,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(args),
        )

        assert result["status"] == "insufficient_data"
        assert result["facts"]["sleep"] is None
        assert result["facts"]["sleep_adapter_reason"] == "no_complete_sleep_summary"
        assert result["recommendation"] == {
            "maximum_additional_mg": None,
            "suggested_additional_mg": None,
            "basis": "unavailable",
        }
        assert result["reason"] == "missing_sleep"
        assert result["facts"]["target_event"]["start"].startswith(day.isoformat())
        assert result["facts"]["caffeine_intake"]["status"] == "incomplete"
        assert result["facts"]["consumed_today_mg"] is None
        assert result["facts"]["total_intake_complete"] is False

    async def test_callers_cannot_override_stored_daily_intake(
        self,
        mcp_client,
        store_factory,
        pinned_tz,
    ):
        _, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["consumed_today_mg"] = 0
        args["total_intake_complete"] = True

        with pytest.raises(ToolError):
            await mcp_client.call_tool("get_caffeine_proposal", args)

    async def test_unconfirmed_baseline_returns_only_an_upper_bound(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(store_factory, day=day)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["baseline_confirmed_at"] = None

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(args),
        )

        assert result["status"] == "proposal"
        assert result["recommendation"]["maximum_additional_mg"] == 200
        assert result["recommendation"]["suggested_additional_mg"] is None
        assert result["recommendation"]["basis"] == "upper_bound_only"
        assert result["reason"] == "personal_event_baseline_unavailable"

    async def test_stale_baseline_returns_only_an_upper_bound(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(store_factory, day=day)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["baseline_confirmed_at"] = (event_start - dt.timedelta(days=2)).isoformat()

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(args),
        )

        assert result["status"] == "proposal"
        assert result["facts"]["personal_event_baseline"]["freshness"] == "stale"
        assert result["recommendation"] == {
            "maximum_additional_mg": 200,
            "suggested_additional_mg": None,
            "basis": "upper_bound_only",
        }
        assert result["reason"] == "personal_event_baseline_unavailable"

    async def test_future_baseline_confirmation_is_not_current_evidence(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
        monkeypatch,
    ):
        now = server_module._utc_now().astimezone(pinned_tz)
        event_start = dt.datetime.combine(
            now.date(),
            dt.time(13),
            tzinfo=pinned_tz,
        )
        target_sleep = event_start + dt.timedelta(hours=10)
        monkeypatch.setattr(server_module, "_today_local", lambda: event_start.date())
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(event_start.date().isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(store_factory, day=event_start.date())
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["baseline_confirmed_at"] = (now + dt.timedelta(hours=1)).isoformat()

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(args),
        )

        assert result["status"] == "proposal"
        assert result["facts"]["personal_event_baseline"]["freshness"] == "stale"
        assert result["recommendation"] == {
            "maximum_additional_mg": 200,
            "suggested_additional_mg": None,
            "basis": "upper_bound_only",
        }
        assert result["reason"] == "personal_event_baseline_unavailable"

    @pytest.mark.parametrize("missing_field", ["cutoff_before_sleep_hours", "contraindications"])
    async def test_cutoff_and_contraindication_confirmation_are_required(
        self,
        mcp_client,
        store_factory,
        pinned_tz,
        missing_field,
    ):
        _, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        del args[missing_field]

        with pytest.raises(ToolError):
            await mcp_client.call_tool("get_caffeine_proposal", args)

    async def test_missing_intended_consumption_time_fails_closed(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(store_factory, day=day)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["intended_consumption_at"] = None

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(args),
        )

        assert result["status"] == "insufficient_data"
        assert result["reason"] == "missing_timing"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert result["recommendation"]["suggested_additional_mg"] is None

    async def test_contraindication_returns_noop_without_a_numeric_proposal(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(store_factory, day=day)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["contraindications"] = ["relevant_medication_or_condition"]

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(args),
        )

        assert result["status"] == "noop"
        assert result["reason"] == "clinician_guidance_required"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert result["recommendation"]["suggested_additional_mg"] is None

    async def test_late_intended_consumption_respects_sleep_cutoff(
        self,
        mcp_client,
        call_tool,
        mcp_env,
        store_factory,
        pinned_tz,
    ):
        day, event_start, target_sleep = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )
        mcp_env.add_sleep_summary(day.isoformat(), duration_minutes=374)
        _seed_confirmed_caffeine(store_factory, day=day)
        args = _proposal_args(
            event_id,
            event_start_local=event_start,
            target_sleep_local=target_sleep,
        )
        args["intended_consumption_at"] = (event_start + dt.timedelta(hours=5)).isoformat()

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _trusted_caffeine(args),
        )

        assert result["status"] == "noop"
        assert result["reason"] == "within_sleep_cutoff"
        assert result["recommendation"]["maximum_additional_mg"] is None
        assert result["recommendation"]["suggested_additional_mg"] is None

    async def test_timezone_sensitive_inputs_require_explicit_offsets(
        self,
        mcp_client,
        store_factory,
        pinned_tz,
    ):
        _, event_start, _ = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )

        with pytest.raises(ToolError, match="explicit UTC offset"):
            await mcp_client.call_tool(
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": str(event_id),
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "target_sleep_at": "2026-08-02T23:00:00",
                    }
                ),
            )

    async def test_timezone_sensitive_inputs_reject_conflicting_offsets(
        self,
        mcp_client,
        store_factory,
        pinned_tz,
    ):
        _, event_start, _ = _local_times(pinned_tz)
        event_id = _seed_event(
            store_factory,
            start=event_start.astimezone(dt.UTC),
            end=(event_start + dt.timedelta(hours=1)).astimezone(dt.UTC),
        )

        with pytest.raises(ToolError, match="conflicts with the user timezone"):
            await mcp_client.call_tool(
                "get_caffeine_proposal",
                _trusted_caffeine(
                    {
                        "event_id": str(event_id),
                        "personal_daily_limit_mg": 300,
                        "population_status": "confirmed_adult",
                        "product_form": "beverage_or_food",
                        "cutoff_before_sleep_hours": 6,
                        "contraindications": [],
                        "target_sleep_at": "2026-08-07T23:00:00+14:00",
                    }
                ),
            )
