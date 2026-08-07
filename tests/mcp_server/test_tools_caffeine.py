import datetime as dt
import uuid

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

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
    IntakeOutcome,
    IntakeOutcomeStatus,
    NormalizedIntakeItem,
    NutrientFact,
)
from healthmes.nutrition.intake_service import (
    create_interaction,
    persist_outcome,
)
from healthmes.nutrition.repository import (
    persist_caffeine_confirmation,
    persist_daily_confirmation,
    persist_observation,
)
from healthmes.storage import register_storage_object
from healthmes.store import CalendarEventMirror, CalendarSource


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
    return {
        "event_id": str(event_id),
        "personal_daily_limit_mg": 300,
        "population_status": "confirmed_adult",
        "product_form": "beverage_or_food",
        "intended_consumption_at": event_start_local.isoformat(),
        "target_sleep_at": target_sleep_local.isoformat(),
        "personal_event_baseline_mg": 100,
        "baseline_confirmed_at": (event_start_local - dt.timedelta(hours=1)).isoformat(),
        "cutoff_before_sleep_hours": 6,
        "contraindications": [],
    }


def _seed_confirmed_caffeine(
    store_factory,
    *,
    day: dt.date,
    amount_mg: float = 100,
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
        )
        session.commit()
    return observation.observation_id


def _seed_confirmed_text_caffeine(
    store_factory,
    *,
    day: dt.date,
    amount_mg: float,
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
                        origin=EvidenceOrigin.USER,
                    ),
                ),
                confidence=Confidence.HIGH,
            ),
        ),
    )
    with store_factory() as session:
        create_interaction(session, settings, interaction)
        persist_outcome(
            session,
            IntakeOutcome(
                outcome_id=uuid.uuid4(),
                operation_fingerprint="b" * 64,
                interaction_id=interaction_id,
                status=IntakeOutcomeStatus.CONSUMED,
                confirmed_at=observed_at + dt.timedelta(minutes=1),
                source="fixture-user",
                consumed_at=observed_at,
            ),
        )
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
            ),
        )
        session.commit()
    return interaction_id


def _local_times(
    pinned_tz: dt.tzinfo,
    *,
    days_from_today: int = 0,
) -> tuple[dt.date, dt.datetime, dt.datetime]:
    day = dt.datetime.now(pinned_tz).date() + dt.timedelta(days=days_from_today)
    event_start = dt.datetime.combine(day, dt.time(13), tzinfo=pinned_tz)
    target_sleep = dt.datetime.combine(day, dt.time(23), tzinfo=pinned_tz)
    return day, event_start, target_sleep


class TestCaffeineProposalTool:
    @pytest.fixture(autouse=True)
    def _use_iana_timezone(self, mcp_env):
        server_module.set_timezone("Asia/Seoul")

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
            _proposal_args(
                event_id,
                event_start_local=event_start,
                target_sleep_local=target_sleep,
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
        assert result["facts"]["caffeine_intake"]["evidence"][0][
            "observation_id"
        ] == str(observation_id)
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
        interaction_id = _seed_confirmed_text_caffeine(
            store_factory,
            day=day,
            amount_mg=150.1,
        )

        result = await call_tool(
            mcp_client,
            "get_caffeine_proposal",
            _proposal_args(
                event_id,
                event_start_local=event_start,
                target_sleep_local=target_sleep,
            ),
        )

        assert result["status"] == "proposal"
        assert result["facts"]["consumed_today_mg"] == 151
        assert result["facts"]["remaining_daily_allowance_mg"] == 149
        assert result["facts"]["caffeine_intake"]["confirmed_caffeine_mg"] == 150.1
        assert result["facts"]["caffeine_intake"]["consumed_outcome_count"] == 1
        assert result["facts"]["caffeine_intake"]["evidence"][0][
            "interaction_id"
        ] == str(interaction_id)

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
            _proposal_args(
                uuid.uuid4(),
                event_start_local=event_start,
                target_sleep_local=target_sleep,
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
            _proposal_args(
                event_id,
                event_start_local=event_start,
                target_sleep_local=target_sleep,
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

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

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

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

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

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

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
        now = dt.datetime.now(pinned_tz)
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

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

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

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

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

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

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

        result = await call_tool(mcp_client, "get_caffeine_proposal", args)

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
                {
                    "event_id": str(event_id),
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "target_sleep_at": "2026-08-02T23:00:00",
                },
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
                {
                    "event_id": str(event_id),
                    "personal_daily_limit_mg": 300,
                    "population_status": "confirmed_adult",
                    "product_form": "beverage_or_food",
                    "cutoff_before_sleep_hours": 6,
                    "contraindications": [],
                    "target_sleep_at": "2026-08-07T23:00:00+14:00",
                },
            )
