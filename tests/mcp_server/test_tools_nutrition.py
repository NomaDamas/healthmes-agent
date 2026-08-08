import asyncio
import datetime as dt
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from time import sleep

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthmes.mcp_server import server as server_module
from healthmes.nutrition.confirmation_gate import (
    CONFIRMATION_EVENT,
    CONFIRMATION_TOMBSTONE_EVENT,
)
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
from healthmes.nutrition.operation_integrity import result_payload_digest
from healthmes.nutrition.repository import (
    persist_caffeine_confirmation,
    persist_daily_confirmation,
    persist_observation,
)
from healthmes.storage import register_storage_object, run_storage_maintenance
from healthmes.store import WellnessEvent
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
        persist_observation(
            session,
            settings,
            observation,
            request_fingerprint="fixture",
        )
        session.commit()
    return observation


def _untrusted_collision(
    *,
    event_type: str,
    observed_at: dt.datetime,
    source_record_id: str,
) -> WellnessEvent:
    return WellnessEvent(
        event_type=event_type,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=observed_at,
        timezone="Asia/Seoul",
        source_provider=f"untrusted-{event_type}",
        source_device="fixture",
        source_record_id=source_record_id,
        capture_method="manual",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={"invalid_for_healthmes_provider": True},
        derived_from=None,
    )


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
    with store_factory() as session:
        for event_type in (
            "nutrition.observation.v1",
            "nutrition.confirmation.v1",
            "nutrition.daily-confirmation.v1",
        ):
            session.add(
                _untrusted_collision(
                    event_type=event_type,
                    observed_at=dt.datetime(2026, 8, 6, 1, 1, tzinfo=dt.UTC),
                    source_record_id=str(observation.observation_id),
                )
            )
        session.commit()

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
    assert evidence["observations"][0]["observation_id"] == str(observation.observation_id)
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
            operation_fingerprint="e" * 64,
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
            operation_fingerprint="c" * 64,
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


async def _stage_photo_confirmation(
    mcp_client,
    call_tool,
    store_factory,
) -> tuple[NutritionObservation, str, dict[str, object]]:
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    operation_id = str(uuid.uuid4())
    staged = await call_tool(
        mcp_client,
        "confirm_photo_caffeine_observation",
        {
            "operation_id": operation_id,
            "observation_id": str(observation.observation_id),
            "status": "confirmed",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    assert staged["status"] == "confirmation_required"
    return observation, operation_id, staged


def _nutrition_review_items(*, serving_unit: str = "ml") -> list[dict[str, object]]:
    nutrient_values = {
        "energy": (90, "kcal"),
        "protein": (4, "g"),
        "carbohydrate": (13, "g"),
        "fat": (2, "g"),
        "fiber": (0, "g"),
        "sugar": (11, "g"),
        "sodium": (80, "mg"),
        "caffeine": (100, "mg"),
    }
    return [
        {
            "item_index": 0,
            "name": "small latte",
            "intake_type": "beverage",
            "serving": {
                "kind": "exact",
                "unit": serving_unit,
                "exact": 250,
                "estimation_basis": "owner_correction",
            },
            "nutrients": [
                {
                    "nutrient": nutrient,
                    "amount": {
                        "kind": "exact",
                        "unit": unit,
                        "exact": amount,
                        "estimation_basis": "owner_correction",
                    },
                    "confidence": "high",
                }
                for nutrient, (amount, unit) in nutrient_values.items()
            ],
            "confidence": "high",
        }
    ]


async def test_trusted_mcp_confirmation_flow(
    mcp_client,
    call_tool,
    confirm_nutrition_action,
    resolve_nutrition_confirmation,
    store_factory,
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    observation_arguments = {
        "operation_id": str(uuid.uuid4()),
        "observation_id": str(observation.observation_id),
        "status": "confirmed",
        "items": [{"item_index": 0, "caffeine_mg": 175}],
    }
    staged_observation = await call_tool(
        mcp_client,
        "confirm_photo_caffeine_observation",
        observation_arguments,
    )
    assert staged_observation["status"] == "confirmation_required"
    assert staged_observation["operation_id"] == observation_arguments["operation_id"]
    before_confirmation = await call_tool(
        mcp_client,
        "get_known_caffeine_intake_for_day",
        {"date": "2026-08-06"},
    )
    assert before_confirmation["confirmed_caffeine_mg"] == 0

    confirmed_observation = await resolve_nutrition_confirmation(
        mcp_client,
        staged_observation,
    )
    retried_observation = await confirm_nutrition_action(
        mcp_client,
        "confirm_photo_caffeine_observation",
        observation_arguments,
    )
    assert confirmed_observation["confirmation_id"] == (observation_arguments["operation_id"])
    assert retried_observation["confirmation_id"] == (observation_arguments["operation_id"])
    changed_observation_arguments = {
        **observation_arguments,
        "items": [{"item_index": 0, "caffeine_mg": 176}],
    }
    with pytest.raises(
        ToolError,
        match="operation_id was already used with different input",
    ):
        await call_tool(
            mcp_client,
            "confirm_photo_caffeine_observation",
            changed_observation_arguments,
        )
    day_arguments = {
        "operation_id": str(uuid.uuid4()),
        "date": "2026-08-06",
        "observation_ids": [str(observation.observation_id)],
        "total_intake_complete": True,
    }
    staged_day = await call_tool(
        mcp_client,
        "confirm_photo_caffeine_day",
        day_arguments,
    )
    confirmed_day = await resolve_nutrition_confirmation(
        mcp_client,
        staged_day,
    )
    retried_day = await confirm_nutrition_action(
        mcp_client,
        "confirm_photo_caffeine_day",
        day_arguments,
    )
    assert confirmed_day["confirmation_id"] == day_arguments["operation_id"]
    assert retried_day["confirmation_id"] == day_arguments["operation_id"]
    changed_day_arguments = {
        **day_arguments,
        "total_intake_complete": False,
    }
    with pytest.raises(
        ToolError,
        match="operation_id was already used with different input",
    ):
        await call_tool(
            mcp_client,
            "confirm_photo_caffeine_day",
            changed_day_arguments,
        )

    known = await call_tool(
        mcp_client,
        "get_known_caffeine_intake_for_day",
        {"date": "2026-08-06"},
    )
    assert known["status"] == "known"
    assert known["confirmed_caffeine_mg"] == 175


async def test_mcp_nutrition_mutations_require_operation_ids(
    mcp_client,
    store_factory,
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
    review_arguments = {
        "observation_id": str(observation.observation_id),
        "status": "confirmed",
        "items": [],
    }
    day_arguments = {
        "date": "2026-08-06",
        "observation_ids": [str(observation.observation_id)],
        "total_intake_complete": True,
    }

    for tool_name, arguments in (
        ("confirm_photo_caffeine_observation", observation_arguments),
        ("review_photo_nutrition_observation", review_arguments),
        ("confirm_photo_caffeine_day", day_arguments),
    ):
        with pytest.raises(ToolError, match="operation_id"):
            await mcp_client.call_tool(
                tool_name,
                arguments,
            )


async def test_mcp_write_fingerprints_use_canonical_uuid_values(
    mcp_client,
    confirm_nutrition_action,
    store_factory,
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    operation_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    first_arguments = {
        "operation_id": str(operation_id).upper(),
        "observation_id": str(observation.observation_id).upper(),
        "status": "confirmed",
        "items": [{"item_index": 0, "caffeine_mg": 175}],
    }
    retry_arguments = {
        **first_arguments,
        "operation_id": str(operation_id),
        "observation_id": str(observation.observation_id),
    }

    first = await confirm_nutrition_action(
        mcp_client,
        "confirm_photo_caffeine_observation",
        first_arguments,
    )
    retry = await confirm_nutrition_action(
        mcp_client,
        "confirm_photo_caffeine_observation",
        retry_arguments,
    )

    assert first["confirmation_id"] == retry["confirmation_id"] == str(operation_id)


async def test_mcp_daily_confirmation_rejects_canonical_uuid_duplicates(
    mcp_client,
    call_tool,
    store_factory,
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    arguments = {
        "operation_id": str(uuid.uuid4()),
        "date": "2026-08-06",
        "observation_ids": [
            str(observation.observation_id),
            str(observation.observation_id).upper(),
        ],
        "total_intake_complete": True,
    }

    with pytest.raises(ToolError, match="must not contain duplicates"):
        await call_tool(
            mcp_client,
            "confirm_photo_caffeine_day",
            arguments,
        )


async def test_trusted_mcp_full_nutrition_review_is_visible(
    mcp_client,
    call_tool,
    confirm_nutrition_action,
    store_factory,
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    items = _nutrition_review_items()
    arguments = {
        "operation_id": str(uuid.uuid4()),
        "observation_id": str(observation.observation_id),
        "status": "corrected",
        "items": items,
    }
    result = await confirm_nutrition_action(
        mcp_client,
        "review_photo_nutrition_observation",
        arguments,
    )
    retry = await confirm_nutrition_action(
        mcp_client,
        "review_photo_nutrition_observation",
        arguments,
    )
    assert result["review_status"] == "corrected"
    assert retry["review_id"] == result["review_id"]
    changed_arguments = {
        **arguments,
        "status": "rejected",
        "items": [],
    }
    with pytest.raises(
        ToolError,
        match="operation_id was already used with different input",
    ):
        await call_tool(
            mcp_client,
            "review_photo_nutrition_observation",
            changed_arguments,
        )

    observations = await call_tool(
        mcp_client,
        "get_recent_nutrition_observations",
        {},
    )
    review = observations["observations"][0]["latest_nutrition_review"]
    assert review["status"] == "corrected"
    assert review["items"][0]["name"] == "small latte"


async def test_mcp_nutrition_review_rejects_oversized_estimate_fields(
    mcp_client, call_tool, store_factory
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    arguments = {
        "operation_id": str(uuid.uuid4()),
        "observation_id": str(observation.observation_id),
        "status": "corrected",
        "items": _nutrition_review_items(serving_unit="x" * 1000),
    }
    with pytest.raises(ToolError, match="unit must contain"):
        await call_tool(
            mcp_client,
            "review_photo_nutrition_observation",
            arguments,
        )


async def test_confirmation_tools_reject_missing_or_tampered_proof(
    mcp_client,
    call_tool,
    store_factory,
):
    server_module.set_timezone("Asia/Seoul")
    settings = server_module._active_settings()
    observation = _seed(
        store_factory,
        settings,
        dt.datetime(2026, 8, 6, 1, tzinfo=dt.UTC),
    )
    staged = await call_tool(
        mcp_client,
        "confirm_photo_caffeine_observation",
        {
            "operation_id": str(uuid.uuid4()),
            "observation_id": str(observation.observation_id),
            "status": "confirmed",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    handle = staged["reply_handle"]
    reply_arguments = {
        "response": f"확인 {handle}",
        "reply_handle": handle,
    }
    with pytest.raises(ToolError, match="trusted_session_proof"):
        await mcp_client.call_tool(
            "resolve_nutrition_confirmation",
            reply_arguments,
        )

    trusted = _trusted(
        "resolve_nutrition_confirmation",
        reply_arguments,
    )
    trusted["response"] = f"취소 {handle}"
    with pytest.raises(ToolError, match="proof"):
        await mcp_client.call_tool(
            "resolve_nutrition_confirmation",
            trusted,
        )


async def test_confirmation_handle_is_never_persisted_in_plaintext(
    mcp_client,
    call_tool,
    store_factory,
):
    _observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )
    handle = staged["reply_handle"]

    with store_factory() as session:
        event = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == CONFIRMATION_EVENT,
                WellnessEvent.source_record_id == operation_id,
            )
        )
        assert event is not None
        persisted = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        assert handle not in persisted
        assert "reply_handle" not in event.payload
        assert event.payload["reply_handle_digest"] != handle


async def test_expired_confirmation_is_terminal_and_never_applies(
    mcp_client,
    call_tool,
    resolve_nutrition_confirmation,
    store_factory,
    monkeypatch,
):
    _observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )
    expires_at = dt.datetime.fromisoformat(str(staged["expires_at"]))
    monkeypatch.setattr(
        server_module,
        "_utc_now",
        lambda: expires_at + dt.timedelta(seconds=1),
    )

    expired = await resolve_nutrition_confirmation(mcp_client, staged)
    replayed = await resolve_nutrition_confirmation(mcp_client, staged)

    assert expired == replayed
    assert expired == {
        "status": "invalidated",
        "action": "photo_caffeine_confirmation",
        "operation_id": operation_id,
        "reason": "confirmation_expired",
    }
    with store_factory() as session:
        applied = list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.confirmation.v1",
                    WellnessEvent.source_record_id == f"caffeine-confirmation:{operation_id}",
                )
            )
        )
        assert applied == []


async def test_cancelled_confirmation_is_terminal_and_never_applies(
    mcp_client,
    call_tool,
    resolve_nutrition_confirmation,
    store_factory,
):
    _observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )

    cancelled = await resolve_nutrition_confirmation(
        mcp_client,
        staged,
        choice="취소",
    )
    replayed = await resolve_nutrition_confirmation(mcp_client, staged)

    assert cancelled == replayed
    assert cancelled == {
        "status": "cancelled",
        "action": "photo_caffeine_confirmation",
        "operation_id": operation_id,
    }
    with store_factory() as session:
        applied = list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.confirmation.v1",
                    WellnessEvent.source_record_id == f"caffeine-confirmation:{operation_id}",
                )
            )
        )
        assert applied == []


async def test_terminal_confirmation_cannot_be_recreated_after_retention_purge(
    mcp_client,
    call_tool,
    resolve_nutrition_confirmation,
    store_factory,
):
    observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )
    await resolve_nutrition_confirmation(
        mcp_client,
        staged,
        choice="취소",
    )
    with store_factory() as session:
        run_storage_maintenance(
            session,
            server_module._active_settings(),
            now=dt.datetime.fromisoformat(str(staged["expires_at"])) + dt.timedelta(seconds=1),
        )
        session.commit()
        confirmation = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == CONFIRMATION_EVENT,
                WellnessEvent.source_record_id == operation_id,
            )
        )
        tombstone = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == CONFIRMATION_TOMBSTONE_EVENT,
                WellnessEvent.source_record_id == operation_id,
            )
        )
        assert confirmation is None
        assert tombstone is not None
        assert set(tombstone.payload) == {
            "schema_version",
            "action_id",
            "action",
            "request_sha256",
            "state",
            "result_sha256",
            "completed_at",
        }

    with pytest.raises(ToolError, match="terminal and cannot be reused"):
        await call_tool(
            mcp_client,
            "confirm_photo_caffeine_observation",
            {
                "operation_id": operation_id,
                "observation_id": str(observation.observation_id),
                "status": "confirmed",
                "items": [{"item_index": 0, "caffeine_mg": 180}],
            },
        )


async def test_confirmation_rejects_tampered_snapshot(
    mcp_client,
    call_tool,
    resolve_nutrition_confirmation,
    store_factory,
):
    _observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )
    with store_factory() as session:
        event = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == CONFIRMATION_EVENT,
                WellnessEvent.source_record_id == operation_id,
            )
        )
        assert event is not None
        payload = deepcopy(event.payload)
        payload["snapshot"]["arguments"]["items"][0]["caffeine_mg"] = 999
        event.payload = payload
        session.commit()

    with pytest.raises(ToolError, match="stored nutrition confirmation is malformed"):
        await resolve_nutrition_confirmation(mcp_client, staged)


async def test_confirmation_handle_rejects_snapshot_and_digest_rewrite(
    mcp_client,
    call_tool,
    resolve_nutrition_confirmation,
    store_factory,
):
    _observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )
    with store_factory() as session:
        event = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == CONFIRMATION_EVENT,
                WellnessEvent.source_record_id == operation_id,
            )
        )
        assert event is not None
        payload = deepcopy(event.payload)
        payload["snapshot"]["arguments"]["items"][0]["caffeine_mg"] = 999
        payload["snapshot_sha256"] = result_payload_digest(
            {
                "action": payload["action"],
                "snapshot": payload["snapshot"],
            }
        )
        event.payload = payload
        session.commit()

    with pytest.raises(
        ToolError,
        match="stored nutrition confirmation handle is malformed",
    ):
        await resolve_nutrition_confirmation(mcp_client, staged)


async def test_confirmation_rejects_tampered_terminal_result(
    mcp_client,
    call_tool,
    resolve_nutrition_confirmation,
    store_factory,
):
    _observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )
    await resolve_nutrition_confirmation(mcp_client, staged)
    with store_factory() as session:
        event = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == CONFIRMATION_EVENT,
                WellnessEvent.source_record_id == operation_id,
            )
        )
        assert event is not None
        payload = deepcopy(event.payload)
        payload["result"]["confirmation_status"] = "rejected"
        event.payload = payload
        session.commit()

    with pytest.raises(
        ToolError,
        match="stored nutrition confirmation result is malformed",
    ):
        await resolve_nutrition_confirmation(mcp_client, staged)


async def test_concurrent_confirmation_resolvers_apply_exactly_once(
    mcp_client,
    call_tool,
    store_factory,
    monkeypatch,
):
    _observation, operation_id, staged = await _stage_photo_confirmation(
        mcp_client,
        call_tool,
        store_factory,
    )
    handle = str(staged["reply_handle"])
    arguments = {
        "response": f"확인 {handle}",
        "reply_handle": handle,
    }
    apply_started = threading.Event()
    release_apply = threading.Event()
    apply_count = 0
    count_lock = threading.Lock()
    apply_action = server_module._apply_confirmed_nutrition_action

    def blocked_apply(session, confirmation):
        nonlocal apply_count
        with count_lock:
            apply_count += 1
        apply_started.set()
        assert release_apply.wait(timeout=5)
        return apply_action(session, confirmation)

    def resolve_once() -> dict[str, object]:
        trusted = _trusted("resolve_nutrition_confirmation", arguments)
        return asyncio.run(
            server_module.resolve_nutrition_confirmation(
                response=arguments["response"],
                reply_handle=handle,
                trusted_session_proof=trusted["trusted_session_proof"],
            )
        )

    monkeypatch.setattr(
        server_module,
        "_apply_confirmed_nutrition_action",
        blocked_apply,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(resolve_once)
        assert apply_started.wait(timeout=5)
        second = pool.submit(resolve_once)
        sleep(0.05)
        assert not second.done()
        release_apply.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result == second_result
    assert apply_count == 1
    with store_factory() as session:
        applied = list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.confirmation.v1",
                    WellnessEvent.source_record_id == f"caffeine-confirmation:{operation_id}",
                )
            )
        )
        assert len(applied) == 1
