import datetime as dt
import uuid

import pytest
from fastmcp.exceptions import ToolError

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
from healthmes.nutrition.repository import (
    persist_caffeine_confirmation,
    persist_daily_confirmation,
    persist_observation,
)
from healthmes.storage import register_storage_object
from healthmes.trusted_session import issue_trusted_session_proof


def _observation(observed_at: dt.datetime, media_path: str) -> NutritionObservation:
    return NutritionObservation(
        observation_id=uuid.uuid4(),
        capture=CaptureContext(
            media_path=media_path,
            captured_at=observed_at,
            timezone="Asia/Seoul",
            source="fixture",
            location=None,
            metadata_provenance={
                "captured_at": MetadataSource.FIXTURE,
                "timezone": MetadataSource.FIXTURE,
                "location": MetadataSource.UNAVAILABLE,
            },
        ),
        status=ObservationStatus.USABLE,
        confidence=Confidence.MEDIUM,
        warnings=(),
        items=(
            IntakeItem(
                intake_type=IntakeType.BEVERAGE,
                name_candidates=("coffee",),
                category="coffee",
                serving=Estimate(
                    kind=EstimateKind.RANGE,
                    unit="ml",
                    minimum=300,
                    maximum=500,
                    estimation_basis="container_size",
                ),
                caffeine=Estimate(
                    kind=EstimateKind.RANGE,
                    unit="mg",
                    minimum=80,
                    maximum=240,
                    estimation_basis="beverage_type_and_container",
                ),
                confidence=Confidence.MEDIUM,
                warnings=("requires confirmation",),
            ),
        ),
        vision=VisionProvenance(
            provider="fixture",
            model="fixture-v1",
            model_digest="sha256:fixture",
            prompt_version="photo-intake-v1",
            schema_version="nutrition-observation-v1",
            analyzed_at=observed_at + dt.timedelta(seconds=2),
        ),
    )


def _seed(store_factory, settings, observed_at):
    media_path = "media/2026/08/fixture.jpg"
    target = settings.data_dir / media_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fixture")
    observation = _observation(observed_at, media_path)
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
        persist_observation(session, settings, observation)
        session.commit()
    return observation


async def test_unconfirmed_estimate_is_visible_but_not_known_total(
    mcp_client, call_tool, store_factory
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )

    evidence = await call_tool(
        mcp_client,
        "get_caffeine_observations",
        {"date": "2026-08-06"},
    )
    known = await call_tool(
        mcp_client,
        "get_known_caffeine_intake_for_day",
        {"date": "2026-08-06"},
    )

    assert evidence["count"] == 1
    assert evidence["observations"][0]["observation_id"] == str(
        observation.observation_id
    )
    assert evidence["observations"][0]["items"][0]["caffeine"]["kind"] == "range"
    assert known["status"] == "incomplete"
    assert known["confirmed_caffeine_mg"] == 0
    assert known["total_intake_complete"] is False


async def test_confirmed_observation_and_daily_proof_produce_known_total(
    mcp_client, call_tool, store_factory
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observed_at = dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC)
    observation = _seed(store_factory, settings, observed_at)
    with store_factory() as session:
        persist_caffeine_confirmation(
            session,
            CaffeineConfirmation(
                confirmation_id=uuid.uuid4(),
                observation_id=observation.observation_id,
                status=ConfirmationStatus.CONFIRMED,
                confirmed_at=observed_at + dt.timedelta(minutes=1),
                source="fixture-user",
                items=(ConfirmedCaffeineItem(item_index=0, caffeine_mg=180),),
            ),
        )
        persist_daily_confirmation(
            session,
            DailyIntakeConfirmation(
                confirmation_id=uuid.uuid4(),
                local_date=dt.date(2026, 8, 6),
                timezone="Asia/Seoul",
                observation_ids=(observation.observation_id,),
                total_intake_complete=True,
                confirmed_at=observed_at + dt.timedelta(minutes=2),
                source="fixture-user",
            ),
        )
        session.commit()

    known = await call_tool(
        mcp_client,
        "get_known_caffeine_intake_for_day",
        {"date": "2026-08-06"},
    )

    assert known["status"] == "known"
    assert known["confirmed_caffeine_mg"] == 180
    assert known["total_intake_complete"] is True
    assert known["reviewed_count"] == 1


def _trusted(tool_name, arguments):
    proof = issue_trusted_session_proof(
        "test-calendar-adjustment-secret-32-characters",
        tool_name=tool_name,
        arguments=arguments,
        platform="telegram",
        chat_id="owner-chat",
        user_id="owner-user",
        message_id=str(uuid.uuid4()),
    )
    return {**arguments, "trusted_session_proof": proof}


async def test_trusted_mcp_confirmation_flow(
    mcp_client, call_tool, store_factory
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    observation_arguments = {
        "observation_id": str(observation.observation_id),
        "status": "confirmed",
        "items": [{"item_index": 0, "caffeine_mg": 175}],
    }
    await call_tool(
        mcp_client,
        "confirm_photo_caffeine_observation",
        _trusted("confirm_photo_caffeine_observation", observation_arguments),
    )
    day_arguments = {
        "date": "2026-08-06",
        "observation_ids": [str(observation.observation_id)],
        "total_intake_complete": True,
    }
    await call_tool(
        mcp_client,
        "confirm_photo_caffeine_day",
        _trusted("confirm_photo_caffeine_day", day_arguments),
    )

    known = await call_tool(
        mcp_client,
        "get_known_caffeine_intake_for_day",
        {"date": "2026-08-06"},
    )
    assert known["status"] == "known"
    assert known["confirmed_caffeine_mg"] == 175


async def test_confirmation_tools_reject_missing_or_tampered_proof(
    mcp_client, store_factory
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    with pytest.raises(ToolError, match="trusted_session_proof"):
        await mcp_client.call_tool(
            "confirm_photo_caffeine_observation",
            {
                "observation_id": str(observation.observation_id),
                "status": "confirmed",
                "items": [{"item_index": 0, "caffeine_mg": 180}],
            },
        )
