import hashlib
import json
import math
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from healthmes.storage.service import update_retention_policy
from healthmes.store import WellnessEvent
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_QUERY_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
    OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
    commit_open_wearables_snapshot,
    latest_retained_open_wearables_query_snapshot,
    latest_retained_open_wearables_snapshot,
    open_wearables_daily_execution_scope_digest,
    open_wearables_retention_policy_binding,
    persist_open_wearables_observation,
    persist_open_wearables_query_snapshot,
    persist_open_wearables_snapshot,
    wearable_query_snapshot_from_event,
    wearable_snapshot_from_event,
)
from healthmes.wearables.whoop_recovery import (
    WHOOP_RECOVERY_ALGORITHM,
    calculate_whoop_recovery_package,
)

DAY = date(2026, 8, 10)
NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
WHOOP_START = datetime(2026, 8, 10, tzinfo=UTC)
WHOOP_END = datetime(2026, 8, 11, tzinfo=UTC)
WHOOP_PARAMETERS = {"as_of": "2026-08-10"}
PROVIDER_BINDING_A = "sha256:" + ("a" * 64)
PROVIDER_BINDING_B = "sha256:" + ("b" * 64)
PROVIDER_BINDING_C = "sha256:" + ("c" * 64)


def _whoop_public_result(
    *,
    status: str = "ok",
    recovery_raw_value: float = 70,
    day_strain_raw_value: float = 12,
    timezone: str = "UTC",
) -> dict:
    if status == "insufficient_data":
        return calculate_whoop_recovery_package(
            [],
            [],
            as_of=date(2026, 8, 10),
            timezone=timezone,
        ).public
    provenance = _whoop_private_provenance(
        recovery_raw_value=recovery_raw_value,
        day_strain_raw_value=day_strain_raw_value,
    )
    rows: dict[str, list[dict]] = {
        "recovery": [],
        "day_strain": [],
    }
    for item in provenance:
        components = {
            "cycle_id": {"qualifier": item["cycle_id"]}
        }
        if item["metric"] == "day_strain":
            components["cycle_updated_at"] = {
                "qualifier": item["revision_at"]
            }
        rows[item["metric"]].append(
            {
                "id": item["record_id"],
                "provider": "whoop",
                "category": item["metric"],
                "recorded_at": item["observed_at"],
                "value": item["raw_value"],
                "components": components,
            }
        )
    return calculate_whoop_recovery_package(
        rows["recovery"],
        rows["day_strain"],
        as_of=date(2026, 8, 10),
        timezone=timezone,
    ).public


def _whoop_private_provenance(
    *,
    recovery_record_id: str = "recovery-row-1",
    recovery_raw_value: float = 70,
    day_strain_record_id: str = "day-strain-row-1",
    day_strain_raw_value: float = 12,
) -> list[dict]:
    return [
        {
            "source_provider": "open-wearables",
            "upstream_provider": "whoop",
            "record_id": recovery_record_id,
            "resource_type": "health_score",
            "metric": "recovery",
            "metric_definition": "whoop.recovery-score.0-to-100.v1",
            "observed_at": "2026-08-10T08:00:00+00:00",
            "revision_at": "2026-08-10T08:00:00+00:00",
            "cycle_id": "cycle-1",
            "raw_value": recovery_raw_value,
            "schema_version": 1,
            "derived_by": WHOOP_RECOVERY_ALGORITHM,
        },
        {
            "source_provider": "open-wearables",
            "upstream_provider": "whoop",
            "record_id": day_strain_record_id,
            "resource_type": "health_score",
            "metric": "day_strain",
            "metric_definition": (
                "whoop.cycle-cumulative-day-strain.0-to-21.v1"
            ),
            "observed_at": "2026-08-10T09:00:00+00:00",
            "revision_at": "2026-08-10T10:00:00+00:00",
            "cycle_id": "cycle-1",
            "raw_value": day_strain_raw_value,
            "schema_version": 1,
            "derived_by": WHOOP_RECOVERY_ALGORITHM,
        },
    ]


def _persist_whoop_package(
    session,
    *,
    result: dict | None = None,
    private_provenance: list[dict] | None = None,
    now: datetime = NOW,
    provider_binding_digest: str | None = None,
):
    return persist_open_wearables_query_snapshot(
        session,
        capability="wearable.whoop-recovery-package",
        start=WHOOP_START,
        end=WHOOP_END,
        timezone="UTC",
        parameters=WHOOP_PARAMETERS,
        result=result if result is not None else _whoop_public_result(),
        private_provenance=(
            private_provenance
            if private_provenance is not None
            else _whoop_private_provenance()
        ),
        collected_at=now,
        now=now,
        provider_binding_digest=provider_binding_digest,
    )


def _whoop_unrepresentable_calculation(reason: str):
    recovery_rows = [
        {
            "id": "recovery-row-1",
            "provider": "whoop",
            "category": "recovery",
            "recorded_at": "2026-08-10T08:00:00+00:00",
            "value": 70,
            "components": {
                "cycle_id": {"qualifier": "cycle-1"},
            },
        }
    ]
    day_strain_rows = [
        {
            "id": "day-strain-row-1",
            "provider": "whoop",
            "category": "day_strain",
            "recorded_at": "2026-08-10T09:00:00+00:00",
            "value": 12,
            "components": {
                "cycle_id": {"qualifier": "cycle-1"},
                "cycle_updated_at": {
                    "qualifier": "2026-08-10T10:00:00+00:00"
                },
            },
        }
    ]
    if reason == "source_record_id_missing":
        recovery_rows[0].pop("id")
    elif reason == "unparseable_raw_value":
        recovery_rows[0]["value"] = "not-a-number"
    elif reason == "unparseable_recorded_at":
        recovery_rows[0]["recorded_at"] = "not-a-timestamp"
    elif reason == "unparseable_cycle_updated_at":
        day_strain_rows[0]["components"]["cycle_updated_at"] = {
            "qualifier": "not-a-timestamp"
        }
    elif reason == "ambiguous_latest_row":
        duplicate = deepcopy(recovery_rows[0])
        duplicate["id"] = "recovery-row-2"
        recovery_rows.append(duplicate)
    else:
        raise AssertionError(f"unknown WHOOP failure reason: {reason}")
    return calculate_whoop_recovery_package(
        recovery_rows,
        day_strain_rows,
        as_of=date(2026, 8, 10),
        timezone="UTC",
    )


def _context(*, stress: int = 42) -> dict:
    return {
        "status": "ok",
        "date": DAY.isoformat(),
        "stress": {
            "status": "ok",
            "value": stress,
            "recorded_at": "2026-08-10T08:00:00+00:00",
        },
        "source_refs": [
            {
                "domain": "wearable",
                "record_id": "score-1",
                "source_provider": "open-wearables",
                "upstream_provider": "garmin",
                "resource_type": "health_score",
                "observed_at": "2026-08-10T08:00:00+00:00",
                "schema_version": 1,
            }
        ],
        "evidence_ids": ["score-1"],
    }


def _count(session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(WellnessEvent)
        .where(
            WellnessEvent.event_type
            == OPEN_WEARABLES_SNAPSHOT_EVENT_TYPE
        )
    )


def _observation_count(session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(WellnessEvent)
        .where(
            WellnessEvent.event_type
            == OPEN_WEARABLES_OBSERVATION_EVENT_TYPE
        )
    )


def _query_count(session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(WellnessEvent)
        .where(
            WellnessEvent.event_type
            == OPEN_WEARABLES_QUERY_EVENT_TYPE
        )
    )


def test_persist_snapshot_uses_local_event_identity_and_retention(session) -> None:
    event = persist_open_wearables_snapshot(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="Asia/Seoul",
        collected_at=NOW,
        now=NOW,
    )

    assert event.source_provider == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
    assert event.source_record_id.startswith(f"snapshot:{DAY.isoformat()}:")
    assert event.timezone == "Asia/Seoul"
    assert event.sensitivity == "wearable"
    assert event.consent_scope == "personal"
    assert event.retention_policy_id is not None
    assert event.expires_at == datetime(2026, 9, 8, 15, tzinfo=UTC)
    assert event.payload["normalized_context"] == _context()
    assert event.payload["upstream_provenance"] == {
        "source_refs": _context()["source_refs"],
        "evidence_ids": ["score-1"],
    }
    assert event.payload["window"] == {
        "start": "2026-08-09T15:00:00+00:00",
        "end": "2026-08-10T15:00:00+00:00",
    }
    assert event.payload["collected_at"] == NOW.isoformat()
    assert len(event.payload["content_digest"]) == 64


def test_exact_collection_retry_is_idempotent(session) -> None:
    first = persist_open_wearables_snapshot(
        session,
        normalized_context={"date": DAY.isoformat(), "stress": 42, "hrv": 55},
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    second = persist_open_wearables_snapshot(
        session,
        normalized_context={"hrv": 55, "stress": 42, "date": DAY.isoformat()},
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )

    assert second.id == first.id
    assert _count(session) == 1
    assert second.recorded_at == NOW


def test_same_content_at_a_later_collection_reuses_immutable_snapshot(
    session,
) -> None:
    first = persist_open_wearables_snapshot(
        session,
        normalized_context={"date": DAY.isoformat(), "stress": 42},
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    second = persist_open_wearables_snapshot(
        session,
        normalized_context={"stress": 42, "date": DAY.isoformat()},
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW + timedelta(hours=1),
        now=NOW + timedelta(hours=1),
    )

    assert second.id == first.id
    assert _count(session) == 1
    assert _observation_count(session) == 2
    assert first.recorded_at == NOW
    assert second.recorded_at == NOW


def test_snapshot_detaches_caller_and_reader_mutations(session) -> None:
    context = _context()
    event = persist_open_wearables_snapshot(
        session,
        normalized_context=context,
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    context["stress"]["value"] = 999

    loaded = latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW,
    )
    assert loaded is not None
    loaded.normalized_context["stress"]["value"] = 777

    assert event.payload["normalized_context"]["stress"]["value"] == 42


def test_changed_content_creates_new_snapshot_and_latest_returns_it(session) -> None:
    first = persist_open_wearables_snapshot(
        session,
        normalized_context=_context(stress=42),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    second = persist_open_wearables_snapshot(
        session,
        normalized_context=_context(stress=58),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW + timedelta(minutes=10),
        now=NOW + timedelta(minutes=10),
    )

    loaded = latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW + timedelta(minutes=11),
    )

    assert first.id != second.id
    assert _count(session) == 2
    assert loaded is not None
    assert loaded.content_event_id == second.id
    assert loaded.normalized_context["stress"]["value"] == 58
    assert loaded.collected_at == NOW + timedelta(minutes=10)
    assert loaded.is_stale(
        now=NOW + timedelta(hours=2),
        max_age=timedelta(hours=1),
    )


def test_a_b_a_reobservation_returns_latest_a(session) -> None:
    first_a = persist_open_wearables_observation(
        session,
        normalized_context=_context(stress=42),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    b = persist_open_wearables_observation(
        session,
        normalized_context=_context(stress=58),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW + timedelta(minutes=10),
        now=NOW + timedelta(minutes=10),
    )
    latest_a = persist_open_wearables_observation(
        session,
        normalized_context=_context(stress=42),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW + timedelta(minutes=20),
        now=NOW + timedelta(minutes=20),
    )

    loaded = latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW + timedelta(minutes=21),
    )

    assert _count(session) == 2
    assert _observation_count(session) == 3
    assert first_a.content_event_id == latest_a.content_event_id
    assert b.content_event_id != latest_a.content_event_id
    assert first_a.event_id != latest_a.event_id
    assert loaded is not None
    assert loaded.event_id == latest_a.event_id
    assert loaded.normalized_context["stress"]["value"] == 42
    assert loaded.collected_at == NOW + timedelta(minutes=20)


def test_exact_observation_retry_is_idempotent(session) -> None:
    first = persist_open_wearables_observation(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    second = persist_open_wearables_observation(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )

    assert second.event_id == first.event_id
    assert second.content_event_id == first.content_event_id
    assert _count(session) == 1
    assert _observation_count(session) == 1


def test_late_arriving_old_observation_does_not_replace_latest(session) -> None:
    latest = persist_open_wearables_observation(
        session,
        normalized_context=_context(stress=58),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW + timedelta(minutes=20),
        now=NOW + timedelta(minutes=20),
    )
    persist_open_wearables_observation(
        session,
        normalized_context=_context(stress=42),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW + timedelta(minutes=10),
        now=NOW + timedelta(minutes=21),
    )

    loaded = latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW + timedelta(minutes=22),
    )

    assert loaded is not None
    assert loaded.event_id == latest.event_id
    assert loaded.normalized_context["stress"]["value"] == 58


def test_expired_snapshot_is_not_returned(session) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "1d",
        now=NOW,
    )
    event = persist_open_wearables_snapshot(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )

    assert event.expires_at == datetime(2026, 8, 11, tzinfo=UTC)
    assert (
        latest_retained_open_wearables_snapshot(
            session,
            local_day=DAY,
            timezone="UTC",
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )
        is None
    )


def test_expired_duplicate_is_not_revived_by_a_new_collection_time(session) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "1d",
        now=NOW,
    )
    persist_open_wearables_snapshot(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )

    with pytest.raises(ValueError, match="conflicting content"):
        persist_open_wearables_snapshot(
            session,
            normalized_context=_context(),
            local_day=DAY,
            timezone="UTC",
            collected_at=NOW + timedelta(days=1),
            now=NOW + timedelta(days=1),
        )

    assert _count(session) == 1


@pytest.mark.parametrize(
    ("local_day", "expected_hours"),
    [
        (date(2026, 3, 8), 23),
        (date(2026, 11, 1), 25),
    ],
)
def test_observed_window_respects_dst(
    session,
    local_day: date,
    expected_hours: int,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "forever",
        now=NOW,
    )
    event = persist_open_wearables_snapshot(
        session,
        normalized_context={"date": local_day.isoformat(), "status": "ok"},
        local_day=local_day,
        timezone="America/New_York",
        collected_at=NOW,
        now=NOW,
    )
    window = event.payload["window"]
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])

    assert end - start == timedelta(hours=expected_hours)


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_non_finite_json_is_rejected_without_writes(session, bad_value) -> None:
    with pytest.raises(ValueError, match="finite"):
        persist_open_wearables_snapshot(
            session,
            normalized_context={"date": DAY.isoformat(), "stress": bad_value},
            local_day=DAY,
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )

    assert _count(session) == 0


def test_oversized_json_is_rejected_without_writes(session) -> None:
    with pytest.raises(ValueError, match="size limit"):
        persist_open_wearables_snapshot(
            session,
            normalized_context={
                "date": DAY.isoformat(),
                "blob": "x" * 1_000_001,
            },
            local_day=DAY,
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )

    assert _count(session) == 0


@pytest.mark.parametrize(
    "secret_field",
    [
        {"api_key": "must-not-persist"},
        {"nested": {"Authorization": "Bearer must-not-persist"}},
        {"source_refs": [{"refresh-token": "must-not-persist"}]},
        {"open_wearables_api_key": "must-not-persist"},
        {"provider_credentials": "must-not-persist"},
    ],
)
def test_secret_fields_are_rejected_without_leaking_value(
    session,
    secret_field,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        persist_open_wearables_snapshot(
            session,
            normalized_context={
                "date": DAY.isoformat(),
                **secret_field,
            },
            local_day=DAY,
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )

    assert "must-not-persist" not in str(exc_info.value)
    assert _count(session) == 0


def test_invalid_timezone_and_mismatched_scope_are_rejected(session) -> None:
    with pytest.raises(ValueError, match="invalid timezone"):
        persist_open_wearables_snapshot(
            session,
            normalized_context=_context(),
            local_day=DAY,
            timezone="Mars/Olympus",
            collected_at=NOW,
            now=NOW,
        )
    with pytest.raises(ValueError, match="does not match local_day"):
        persist_open_wearables_snapshot(
            session,
            normalized_context={"date": "2026-08-09"},
            local_day=DAY,
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )

    assert _count(session) == 0


def test_naive_times_and_datetime_day_are_rejected(session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        persist_open_wearables_snapshot(
            session,
            normalized_context=_context(),
            local_day=DAY,
            timezone="UTC",
            collected_at=datetime(2026, 8, 10, 12),
            now=NOW,
        )
    with pytest.raises(TypeError, match="local_day must be a date"):
        persist_open_wearables_snapshot(
            session,
            normalized_context=_context(),
            local_day=datetime(2026, 8, 10, tzinfo=UTC),
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )

    assert _count(session) == 0


def test_tampered_payload_is_not_returned_as_fallback(session) -> None:
    event = persist_open_wearables_snapshot(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    event.payload = {
        **event.payload,
        "normalized_context": _context(stress=99),
    }
    session.flush([event])

    assert (
        latest_retained_open_wearables_snapshot(
            session,
            local_day=DAY,
            timezone="UTC",
            now=NOW,
        )
        is None
    )


def test_persist_flushes_but_never_commits_caller_transaction(
    session,
    monkeypatch,
) -> None:
    def fail_commit() -> None:
        raise AssertionError("persist_open_wearables_snapshot must not commit")

    monkeypatch.setattr(session, "commit", fail_commit)
    event = persist_open_wearables_snapshot(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )

    assert event.id is not None
    assert session.in_transaction()
    session.rollback()
    assert _count(session) == 0


def test_commit_snapshot_uses_an_independent_committed_transaction(
    session,
    session_factory,
) -> None:
    with pytest.raises(
        RuntimeError,
        match="distinct physical database connection",
    ):
        commit_open_wearables_snapshot(
            session_factory,
            normalized_context=_context(),
            local_day=DAY,
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )

    assert _count(session) == 0
    assert _observation_count(session) == 0


def test_independent_writer_never_commits_caller_static_pool_transaction(
    session,
    session_factory,
) -> None:
    sentinel = WellnessEvent(
        event_type="wearable.test-sentinel.v1",
        schema_version=1,
        observed_at=NOW,
        recorded_at=NOW,
        timezone="UTC",
        source_provider="test",
        source_record_id="uncommitted-sentinel",
        payload={},
    )
    session.add(sentinel)
    session.flush()

    with pytest.raises(
        RuntimeError,
        match="distinct physical database connection",
    ):
        commit_open_wearables_snapshot(
            session_factory,
            normalized_context=_context(),
            local_day=DAY,
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )
    session.rollback()

    with session_factory() as observer:
        assert observer.scalar(
            select(func.count()).select_from(WellnessEvent)
        ) == 0


def test_bounded_query_snapshot_is_stable_retained_and_tamper_evident(
    session,
) -> None:
    start = datetime(2026, 8, 10, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    result = {
        "status": "ok",
        "records": [
            {
                "timestamp": start.isoformat(),
                "series_type": "heart_rate",
                "value": 72,
                "unit": "bpm",
            }
        ],
        "limitations": [],
        "coverage": {"ratio": 1.0},
    }

    first = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters={
            "series_type": "heart_rate",
            "resolution": "1min",
            "cursor": "not-part-of-query-identity",
        },
        result=result,
        collected_at=NOW,
        now=NOW,
    )
    repeated = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters={
            "series_type": "heart_rate",
            "resolution": "1min",
        },
        result=result,
        collected_at=NOW,
        now=NOW,
    )

    assert repeated.event_id == first.event_id
    assert _query_count(session) == 1
    event = session.get(WellnessEvent, first.event_id)
    assert event is not None
    assert event.source_provider == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
    assert event.source_record_id.startswith(
        f"query:{first.query_digest}:"
    )
    assert event.payload["query"]["parameters"] == {
        "series_type": "heart_rate",
        "resolution": "1min",
    }
    assert event.payload["retention_basis_at"] == start.isoformat()
    assert first.retention_basis_at == start
    assert event.expires_at.replace(tzinfo=UTC) == (
        start + timedelta(days=30)
    )
    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    ) == first
    retained = latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters={
            "series_type": "heart_rate",
            "resolution": "1min",
        },
        now=NOW,
    )
    assert retained == first

    event.payload = {
        **event.payload,
        "result": {
            **result,
            "records": [
                {
                    **result["records"][0],
                    "value": 999,
                }
            ],
        },
    }
    session.flush([event])

    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    ) is None
    assert latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters={
            "series_type": "heart_rate",
            "resolution": "1min",
        },
        now=NOW,
    ) is None


@pytest.mark.parametrize(
    ("parameters", "result"),
    (
        (
            {"user_id": "private-user"},
            {
                "status": "ok",
                "records": [],
                "limitations": [],
            },
        ),
        (
            {},
            {
                "status": "ok",
                "records": [{"connection_id": "private-connection"}],
                "limitations": [],
            },
        ),
        (
            {},
            {
                "status": "ok",
                "records": [{"user_connection_id": "private-connection"}],
                "limitations": [],
            },
        ),
        (
            {},
            {
                "status": "ok",
                "records": [{"id": "raw-upstream-record"}],
                "limitations": [],
            },
        ),
        (
            {},
            {
                "status": "ok",
                "records": [{"authorization": "Bearer secret-value"}],
                "limitations": [],
            },
        ),
    ),
)
def test_bounded_query_snapshot_rejects_identity_and_secrets(
    session,
    parameters,
    result,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        persist_open_wearables_query_snapshot(
            session,
            capability="wearable.health-scores",
            start=datetime(2026, 8, 10, tzinfo=UTC),
            end=datetime(2026, 8, 11, tzinfo=UTC),
            timezone="UTC",
            parameters=parameters,
            result=result,
            collected_at=NOW,
            now=NOW,
        )

    assert "private-user" not in str(exc_info.value)
    assert "private-connection" not in str(exc_info.value)
    assert "secret-value" not in str(exc_info.value)
    assert _query_count(session) == 0


def test_bounded_query_snapshot_rejects_expired_observation_window(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "1d",
        now=NOW,
    )

    with pytest.raises(
        ValueError,
        match="outside the normalized retention window",
    ):
        persist_open_wearables_query_snapshot(
            session,
            capability="wearable.health-scores",
            start=NOW - timedelta(days=2),
            end=NOW - timedelta(days=1),
            timezone="UTC",
            parameters={},
            result={
                "status": "ok",
                "records": [
                    {
                        "category": "stress",
                        "recorded_at": (
                            NOW - timedelta(days=2)
                        ).isoformat(),
                        "value": 42,
                    }
                ],
                "limitations": [],
            },
            collected_at=NOW,
            now=NOW,
        )

    assert _query_count(session) == 0


def test_empty_query_snapshot_uses_collection_time_for_retention(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "1d",
        now=NOW,
    )

    snapshot = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.health-scores",
        start=NOW - timedelta(days=2),
        end=NOW - timedelta(days=1),
        timezone="UTC",
        parameters={"date": "2026-08-08"},
        result={
            "status": "empty_success",
            "records": [],
            "limitations": [],
        },
        collected_at=NOW,
        now=NOW,
    )

    event = session.get(WellnessEvent, snapshot.event_id)
    assert event is not None
    assert snapshot.retention_basis_at == NOW
    assert event.observed_at.replace(tzinfo=UTC) == NOW
    assert event.expires_at.replace(tzinfo=UTC) == (
        NOW + timedelta(days=1)
    )
    assert event.payload["query"]["parameters"] == {}


def test_summary_retention_uses_summary_day_not_previous_bedtime(
    session,
) -> None:
    start = datetime(2026, 8, 10, tzinfo=UTC)
    snapshot = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.summaries",
        start=start,
        end=start + timedelta(days=1),
        timezone="UTC",
        parameters={"summary_kind": "sleep"},
        result={
            "status": "ok",
            "records": [
                {
                    "summary_kind": "sleep",
                    "date": "2026-08-10",
                    "start_time": "2026-08-09T23:00:00+00:00",
                    "end_time": "2026-08-10T07:00:00+00:00",
                }
            ],
            "limitations": [],
        },
        collected_at=NOW,
        now=NOW,
    )

    assert snapshot.retention_basis_at == start


def test_body_summary_averaged_retention_uses_full_period_start(
    session,
) -> None:
    period_start = datetime(2026, 8, 4, 12, tzinfo=UTC)
    period_end = datetime(2026, 8, 10, 12, tzinfo=UTC)

    snapshot = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.body-summary",
        start=WHOOP_START,
        end=WHOOP_END,
        timezone="UTC",
        parameters={"average_period": 7},
        result={
            "status": "ok",
            "records": [
                {
                    "record_kind": "body_summary",
                    "summary_as_of": period_end.isoformat(),
                    "averaged": {
                        "resting_heart_rate_bpm": 58,
                        "period_days": 7,
                        "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(),
                    },
                }
            ],
            "limitations": [],
        },
        collected_at=NOW,
        now=NOW,
    )

    assert snapshot.retention_basis_at == period_start


def test_body_summary_slow_only_uses_snapshot_collection_observation(
    session,
) -> None:
    snapshot = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.body-summary",
        start=WHOOP_START,
        end=WHOOP_END,
        timezone="UTC",
        parameters={"average_period": 7},
        result={
            "status": "ok",
            "records": [
                {
                    "record_kind": "body_summary",
                    "summary_as_of": "2000-01-01T00:00:00+00:00",
                    "slow_changing": {"weight_kg": 70},
                }
            ],
            "limitations": [],
        },
        collected_at=NOW,
        now=NOW,
    )

    assert snapshot.retention_basis_at == NOW


def test_whoop_package_v2_separates_public_result_and_private_provenance(
    session,
) -> None:
    snapshot = _persist_whoop_package(session)
    event = session.get(WellnessEvent, snapshot.event_id)

    assert event is not None
    assert event.schema_version == 2
    assert event.payload["schema"] == "healthmes.open-wearables-query.v2"
    assert event.payload["schema_version"] == 2
    assert event.payload["public_result"] == _whoop_public_result()
    assert event.payload["private_provenance"] == (
        _whoop_private_provenance()
    )
    assert "result" not in event.payload
    assert snapshot.schema_version == 2
    assert snapshot.private_provenance_digest == event.payload[
        "private_provenance_digest"
    ]
    assert snapshot.result == _whoop_public_result()
    assert not hasattr(snapshot, "private_provenance")
    assert event.quality_flags == {
        "query_digest": snapshot.query_digest,
        "public_digest": event.payload["public_digest"],
        "semantic_public_digest": event.payload[
            "semantic_public_digest"
        ],
        "private_provenance_digest": (
            snapshot.private_provenance_digest
        ),
        "retention_policy_revision": event.payload[
            "retention_policy"
        ]["revision"],
        "snapshot_schema_version": 2,
    }
    assert event.observed_at.replace(tzinfo=UTC) == datetime(
        2026,
        8,
        10,
        8,
        tzinfo=UTC,
    )
    assert event.expires_at.replace(tzinfo=UTC) == datetime(
        2026,
        9,
        9,
        8,
        tzinfo=UTC,
    )

    loaded = wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    )
    retained = latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.whoop-recovery-package",
        start=WHOOP_START,
        end=WHOOP_END,
        timezone="UTC",
        parameters=WHOOP_PARAMETERS,
        now=NOW,
    )

    assert loaded == snapshot
    assert retained == snapshot
    public_json = json.dumps(loaded.result, sort_keys=True)
    for private_value in (
        "recovery-row-1",
        "day-strain-row-1",
        "cycle-1",
        "metric_definition",
        "raw_value",
        "source_provider",
    ):
        assert private_value not in public_json


def test_whoop_package_identity_binds_semantic_public_private_and_policy(
    session,
) -> None:
    first = _persist_whoop_package(session)
    first_event = session.get(WellnessEvent, first.event_id)
    assert first_event is not None
    original_recorded_at = first_event.recorded_at
    original_expires_at = first_event.expires_at
    reordered = _persist_whoop_package(
        session,
        private_provenance=list(
            reversed(_whoop_private_provenance())
        ),
    )
    changed_public = _whoop_public_result(
        recovery_raw_value=50,
        day_strain_raw_value=12,
    )
    changed_public_provenance = _whoop_private_provenance(
        recovery_raw_value=50,
        day_strain_raw_value=12,
    )
    public_snapshot = _persist_whoop_package(
        session,
        result=changed_public,
        private_provenance=changed_public_provenance,
    )
    changed_private = _whoop_private_provenance(
        recovery_record_id="recovery-row-2"
    )
    private_snapshot = _persist_whoop_package(
        session,
        private_provenance=changed_private,
    )
    later_snapshot = _persist_whoop_package(
        session,
        now=NOW + timedelta(minutes=1),
    )

    assert reordered.event_id == first.event_id
    assert public_snapshot.event_id != first.event_id
    assert private_snapshot.event_id != first.event_id
    assert later_snapshot.event_id == first.event_id
    session.refresh(first_event)
    assert first_event.recorded_at == original_recorded_at
    assert first_event.expires_at == original_expires_at
    assert private_snapshot.query_digest == first.query_digest
    assert (
        private_snapshot.private_provenance_digest
        != first.private_provenance_digest
    )
    assert _query_count(session) == 3


def test_whoop_package_identity_ignores_dynamic_retention_timestamps(
    session,
) -> None:
    policy = open_wearables_retention_policy_binding(session)

    def retained_result(effective_now: datetime) -> dict:
        cutoff = effective_now - timedelta(
            days=int(policy["retention_days"])
        )
        result = _whoop_public_result()
        result["retention_window"] = {
            "effective_now": effective_now.isoformat(),
            "query_start": WHOOP_START.isoformat(),
            "query_end": WHOOP_END.isoformat(),
            "retained_after": cutoff.isoformat(),
            "effective_start": WHOOP_START.isoformat(),
            "effective_start_inclusive": True,
            "effective_end": WHOOP_END.isoformat(),
            "retention_policy": policy,
        }
        return result

    first = _persist_whoop_package(
        session,
        result=retained_result(NOW),
    )
    repeated = _persist_whoop_package(
        session,
        result=retained_result(NOW + timedelta(minutes=5)),
        now=NOW + timedelta(minutes=5),
    )

    assert repeated.event_id == first.event_id
    assert repeated.collected_at == first.collected_at
    assert _query_count(session) == 1


def test_whoop_package_identity_changes_with_retention_policy_revision(
    session,
) -> None:
    first = _persist_whoop_package(session)

    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "90d",
        now=NOW + timedelta(minutes=1),
    )
    changed_policy = _persist_whoop_package(
        session,
        now=NOW + timedelta(minutes=1),
    )

    assert changed_policy.event_id != first.event_id
    assert _query_count(session) == 2


def test_whoop_package_identity_changes_with_query_digest(session) -> None:
    first = _persist_whoop_package(session)
    changed_scope = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.whoop-recovery-package",
        start=datetime(2026, 8, 9, 15, tzinfo=UTC),
        end=datetime(2026, 8, 10, 15, tzinfo=UTC),
        timezone="Asia/Seoul",
        parameters=WHOOP_PARAMETERS,
        result=_whoop_public_result(timezone="Asia/Seoul"),
        private_provenance=_whoop_private_provenance(),
        collected_at=NOW,
        now=NOW,
    )
    first_event = session.get(WellnessEvent, first.event_id)
    changed_event = session.get(WellnessEvent, changed_scope.event_id)

    assert first_event is not None
    assert changed_event is not None
    assert changed_scope.query_digest != first.query_digest
    assert changed_scope.event_id != first.event_id
    assert (
        changed_scope.private_provenance_digest
        == first.private_provenance_digest
    )
    assert (
        changed_event.payload["public_digest"]
        != first_event.payload["public_digest"]
    )
    assert changed_event.source_record_id != first_event.source_record_id
    assert changed_event.source_record_id.startswith(
        f"query:{changed_scope.query_digest}:"
    )
    assert _query_count(session) == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_key",
        "extra_key",
        "too_many_rows",
        "wrong_provider",
        "wrong_metric_definition",
        "duplicate_metric",
        "invalid_raw_value",
        "observation_outside_window",
        "secret_field",
    ),
)
def test_whoop_package_private_provenance_is_exact_and_bounded(
    session,
    mutation: str,
) -> None:
    provenance = _whoop_private_provenance()
    if mutation == "missing_key":
        provenance[0].pop("metric_definition")
    elif mutation == "extra_key":
        provenance[0]["unexpected"] = "value"
    elif mutation == "too_many_rows":
        extra = deepcopy(provenance[0])
        extra["record_id"] = "third-record"
        provenance.append(extra)
    elif mutation == "wrong_provider":
        provenance[0]["upstream_provider"] = "garmin"
    elif mutation == "wrong_metric_definition":
        provenance[0]["metric_definition"] = "generic-score.v1"
    elif mutation == "duplicate_metric":
        provenance[1]["metric"] = "recovery"
        provenance[1]["metric_definition"] = (
            "whoop.recovery-score.0-to-100.v1"
        )
    elif mutation == "invalid_raw_value":
        provenance[0]["raw_value"] = 33.5
    elif mutation == "observation_outside_window":
        provenance[0]["observed_at"] = "2026-08-08T11:59:59+00:00"
    else:
        provenance[0]["api_key"] = "must-not-persist"

    with pytest.raises((TypeError, ValueError)) as exc_info:
        _persist_whoop_package(
            session,
            private_provenance=provenance,
        )

    assert "must-not-persist" not in str(exc_info.value)
    assert _query_count(session) == 0


def test_whoop_package_accepts_cycle_revision_before_observation(
    session,
) -> None:
    provenance = _whoop_private_provenance()
    provenance[1]["revision_at"] = "2026-08-10T08:59:59+00:00"
    result = _whoop_public_result()
    day_strain = result["day_strain"]
    assert isinstance(day_strain, dict)

    snapshot = _persist_whoop_package(
        session,
        result=result,
        private_provenance=provenance,
    )
    event = session.get(WellnessEvent, snapshot.event_id)

    assert event is not None
    assert event.payload["private_provenance"][1]["observed_at"] == (
        "2026-08-10T09:00:00+00:00"
    )
    assert event.payload["private_provenance"][1]["revision_at"] == (
        "2026-08-10T08:59:59+00:00"
    )


@pytest.mark.parametrize(
    "private_key",
    (
        "record_id",
        "source_provider",
        "provider",
        "metric_definition",
        "raw_value",
        "cycle_id",
        "source_refs",
    ),
)
def test_whoop_package_public_result_rejects_private_provenance(
    session,
    private_key: str,
) -> None:
    result = _whoop_public_result()
    result[private_key] = "private-value"

    with pytest.raises(ValueError):
        _persist_whoop_package(session, result=result)

    assert _query_count(session) == 0


@pytest.mark.parametrize(
    "unallowlisted_key",
    (
        "raw_scores",
        "score_breakdown",
        "provider_payload",
    ),
)
def test_whoop_package_public_result_rejects_unallowlisted_top_level_key(
    session,
    unallowlisted_key: str,
) -> None:
    result = _whoop_public_result()
    result[unallowlisted_key] = {"recovery": 70, "day_strain": 12}

    with pytest.raises(
        ValueError,
        match="canonical schema",
    ):
        _persist_whoop_package(session, result=result)

    assert _query_count(session) == 0


@pytest.mark.parametrize(
    "tamper",
    ("extra", "duplicate", "reordered"),
)
def test_whoop_package_limitations_must_be_canonical(
    session,
    tamper: str,
) -> None:
    calculation = _whoop_unrepresentable_calculation(
        "ambiguous_latest_row"
    )
    result = deepcopy(calculation.public)
    limitations = result["limitations"]
    assert isinstance(limitations, list)
    assert len(limitations) > 1
    if tamper == "extra":
        limitations.append("open_wearables_detail_timeout")
    elif tamper == "duplicate":
        limitations.append(limitations[0])
    else:
        limitations.reverse()

    with pytest.raises(
        ValueError,
        match="limitations are inconsistent",
    ):
        _persist_whoop_package(
            session,
            result=result,
            private_provenance=list(calculation.provenance),
        )

    assert _query_count(session) == 0


def test_whoop_package_without_private_rows_uses_collection_for_retention(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "1d",
        now=NOW,
    )
    result = _whoop_public_result(status="insufficient_data")
    result["confidence"] = "low"
    snapshot = _persist_whoop_package(
        session,
        result=result,
        private_provenance=[],
    )
    event = session.get(WellnessEvent, snapshot.event_id)

    assert event is not None
    assert snapshot.retention_basis_at == NOW
    assert event.observed_at.replace(tzinfo=UTC) == NOW
    assert event.expires_at.replace(tzinfo=UTC) == NOW + timedelta(days=1)
    assert event.payload["private_provenance"] == []


@pytest.mark.parametrize(
    "reason",
    (
        "source_record_id_missing",
        "unparseable_raw_value",
        "unparseable_recorded_at",
        "unparseable_cycle_updated_at",
        "ambiguous_latest_row",
    ),
)
def test_whoop_package_accepts_canonical_unrepresentable_failure(
    session,
    reason: str,
) -> None:
    calculation = _whoop_unrepresentable_calculation(reason)

    snapshot = _persist_whoop_package(
        session,
        result=calculation.public,
        private_provenance=list(calculation.provenance),
    )

    assert snapshot.result == calculation.public
    assert _query_count(session) == 1


@pytest.mark.parametrize(
    "reason",
    (
        "source_record_id_missing",
        "unparseable_raw_value",
        "unparseable_recorded_at",
        "unparseable_cycle_updated_at",
        "ambiguous_latest_row",
    ),
)
@pytest.mark.parametrize(
    "tamper",
    ("actions", "represented_signal"),
)
def test_whoop_package_unrepresentable_failure_is_fail_closed(
    session,
    reason: str,
    tamper: str,
) -> None:
    calculation = _whoop_unrepresentable_calculation(reason)
    result = deepcopy(calculation.public)
    if tamper == "actions":
        result["actions"] = []
    else:
        represented_signal = (
            "recovery"
            if reason == "unparseable_cycle_updated_at"
            else "day_strain"
        )
        result[represented_signal]["label"] = "tampered"

    with pytest.raises(
        ValueError,
        match="does not match private provenance",
    ):
        _persist_whoop_package(
            session,
            result=result,
            private_provenance=list(calculation.provenance),
        )

    assert _query_count(session) == 0


def test_whoop_package_preserves_stale_selected_row_provenance(
    session,
) -> None:
    calculation = calculate_whoop_recovery_package(
        [
            {
                "id": "stale-recovery",
                "provider": "whoop",
                "category": "recovery",
                "recorded_at": "2026-08-09T08:00:00+00:00",
                "value": 70,
                "components": {
                    "cycle_id": {"qualifier": "stale-cycle"},
                },
            }
        ],
        [
            {
                "id": "current-day-strain",
                "provider": "whoop",
                "category": "day_strain",
                "recorded_at": "2026-08-10T09:00:00+00:00",
                "value": 12,
                "components": {
                    "cycle_id": {"qualifier": "cycle-1"},
                    "cycle_updated_at": {
                        "qualifier": "2026-08-10T10:00:00+00:00"
                    },
                },
            }
        ],
        as_of=date(2026, 8, 10),
        timezone="UTC",
    )

    snapshot = _persist_whoop_package(
        session,
        result=calculation.public,
        private_provenance=list(calculation.provenance),
    )
    event = session.get(WellnessEvent, snapshot.event_id)

    assert event is not None
    assert calculation.public["recovery"]["reason"] == (
        "not_current_local_day"
    )
    assert event.payload["private_provenance"][0]["record_id"] == (
        "stale-recovery"
    )
    assert snapshot.retention_basis_at == datetime(
        2026,
        8,
        9,
        8,
        tzinfo=UTC,
    )


def test_whoop_package_preserves_row_selected_after_retention_filter(
    session,
) -> None:
    calculation = calculate_whoop_recovery_package(
        [
            {
                "id": "filtered-recovery",
                "provider": "whoop",
                "category": "recovery",
                "recorded_at": "2026-08-10T08:00:00+00:00",
                "value": 70,
                "components": {
                    "cycle_id": {"qualifier": "cycle-1"},
                },
            }
        ],
        [
            {
                "id": "retained-day-strain",
                "provider": "whoop",
                "category": "day_strain",
                "recorded_at": "2026-08-10T09:00:00+00:00",
                "value": 12,
                "components": {
                    "cycle_id": {"qualifier": "cycle-1"},
                    "cycle_updated_at": {
                        "qualifier": "2026-08-10T10:00:00+00:00"
                    },
                },
            }
        ],
        as_of=date(2026, 8, 10),
        timezone="UTC",
        retained_after=datetime(2026, 8, 10, 8, 30, tzinfo=UTC),
    )

    snapshot = _persist_whoop_package(
        session,
        result=calculation.public,
        private_provenance=list(calculation.provenance),
    )
    event = session.get(WellnessEvent, snapshot.event_id)

    assert event is not None
    assert calculation.public["recovery"]["reason"] == (
        "no_whoop_recovery"
    )
    assert event.payload["private_provenance"] == [
        {
            "source_provider": "open-wearables",
            "upstream_provider": "whoop",
            "record_id": "retained-day-strain",
            "resource_type": "health_score",
            "metric": "day_strain",
            "metric_definition": (
                "whoop.cycle-cumulative-day-strain.0-to-21.v1"
            ),
            "observed_at": "2026-08-10T09:00:00+00:00",
            "revision_at": "2026-08-10T10:00:00+00:00",
            "cycle_id": "cycle-1",
            "raw_value": 12.0,
            "schema_version": 1,
            "derived_by": WHOOP_RECOVERY_ALGORITHM,
        }
    ]


@pytest.mark.parametrize(
    "tamper",
    (
        "public_result",
        "private_provenance",
        "payload_digest",
        "quality_flags",
        "derived_from",
        "source_identity",
        "event_schema",
        "retention_basis",
    ),
)
def test_whoop_package_v2_rejects_tampered_payload_and_metadata(
    session,
    tamper: str,
) -> None:
    snapshot = _persist_whoop_package(session)
    event = session.get(WellnessEvent, snapshot.event_id)
    assert event is not None

    if tamper == "public_result":
        payload = deepcopy(event.payload)
        payload["public_result"]["level"] = "priority"
        event.payload = payload
    elif tamper == "private_provenance":
        payload = deepcopy(event.payload)
        payload["private_provenance"][0]["record_id"] = "tampered-row"
        event.payload = payload
    elif tamper == "payload_digest":
        event.payload = {
            **event.payload,
            "private_provenance_digest": "0" * 64,
        }
    elif tamper == "quality_flags":
        event.quality_flags = {
            **event.quality_flags,
            "public_digest": "0" * 64,
        }
    elif tamper == "derived_from":
        event.derived_from = {
            **event.derived_from,
            "mode": "tampered-mode",
        }
    elif tamper == "source_identity":
        event.source_record_id = f"{event.source_record_id}-tampered"
    elif tamper == "event_schema":
        event.schema_version = 1
    else:
        event.observed_at = event.observed_at + timedelta(minutes=1)
    session.flush([event])

    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    ) is None
    assert latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.whoop-recovery-package",
        start=WHOOP_START,
        end=WHOOP_END,
        timezone="UTC",
        parameters=WHOOP_PARAMETERS,
        now=NOW,
    ) is None


def test_v1_query_snapshot_remains_backward_compatible(session) -> None:
    start = datetime(2026, 8, 10, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    snapshot = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters={
            "series_type": "heart_rate",
            "resolution": "1min",
        },
        result={
            "status": "ok",
            "records": [
                {
                    "timestamp": start.isoformat(),
                    "series_type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                }
            ],
            "limitations": [],
        },
        collected_at=NOW,
        now=NOW,
    )
    event = session.get(WellnessEvent, snapshot.event_id)

    assert event is not None
    assert event.schema_version == 1
    assert event.payload["schema"] == "healthmes.open-wearables-query.v1"
    assert "result" in event.payload
    assert "public_result" not in event.payload
    assert "private_provenance" not in event.payload
    assert snapshot.schema_version == 1
    assert snapshot.private_provenance_digest is None
    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    ) == snapshot


def test_whoop_package_rejects_consistent_v1_downgrade(session) -> None:
    snapshot = _persist_whoop_package(session)
    event = session.get(WellnessEvent, snapshot.event_id)

    assert event is not None
    query = deepcopy(event.payload["query"])
    query_digest = event.payload["query_digest"]
    collected_at = event.payload["collected_at"]
    public_result = deepcopy(event.payload["public_result"])
    public_result["records"] = []
    result_digest = hashlib.sha256(
        json.dumps(
            public_result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    observation_digest = hashlib.sha256(
        json.dumps(
            {
                "collected_at": collected_at,
                "query_digest": query_digest,
                "result_digest": result_digest,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    event.schema_version = 1
    event.source_record_id = (
        f"query:{query_digest}:{observation_digest}"
    )
    event.quality_flags = {
        "query_digest": query_digest,
        "result_digest": result_digest,
    }
    event.derived_from = {
        "source": "open-wearables",
        "mode": "bounded-query-mirror",
        "query_digest": query_digest,
    }
    event.payload = {
        "schema": "healthmes.open-wearables-query.v1",
        "query": query,
        "query_digest": query_digest,
        "result": public_result,
        "result_digest": result_digest,
        "collected_at": collected_at,
        "retention_basis_at": collected_at,
        "window": {
            "start": query["start"],
            "end": query["end"],
        },
    }
    session.flush([event])

    assert event.schema_version == 1
    assert event.payload["schema"] == "healthmes.open-wearables-query.v1"
    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    ) is None
    assert latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.whoop-recovery-package",
        start=WHOOP_START,
        end=WHOOP_END,
        timezone="UTC",
        parameters=WHOOP_PARAMETERS,
        now=NOW,
    ) is None


def test_generic_v1_query_rejects_private_provenance(session) -> None:
    with pytest.raises(
        ValueError,
        match="only supported for the WHOOP recovery package",
    ):
        persist_open_wearables_query_snapshot(
            session,
            capability="wearable.health-scores",
            start=datetime(2026, 8, 10, tzinfo=UTC),
            end=datetime(2026, 8, 11, tzinfo=UTC),
            timezone="UTC",
            parameters={},
            result={
                "status": "empty_success",
                "records": [],
                "limitations": [],
            },
            private_provenance=[],
            collected_at=NOW,
            now=NOW,
        )

    assert _query_count(session) == 0


def test_provider_binding_digest_must_be_prefixed_lowercase_sha256(
    session,
) -> None:
    invalid = (
        "a" * 64,
        "sha256:" + ("A" * 64),
        "sha256:" + ("a" * 63),
        "sha512:" + ("a" * 64),
    )

    for value in invalid:
        with pytest.raises(ValueError, match="provider_binding_digest"):
            persist_open_wearables_observation(
                session,
                normalized_context=_context(),
                local_day=DAY,
                timezone="UTC",
                collected_at=NOW,
                now=NOW,
                provider_binding_digest=value,
            )

    assert _count(session) == 0
    assert _observation_count(session) == 0


def test_daily_snapshot_provider_binding_is_private_and_identity_bound(
    session,
) -> None:
    legacy = persist_open_wearables_observation(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
    )
    bound_a = persist_open_wearables_observation(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
        provider_binding_digest=PROVIDER_BINDING_A,
    )
    bound_b = persist_open_wearables_observation(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
        provider_binding_digest=PROVIDER_BINDING_B,
    )

    assert _count(session) == 3
    assert _observation_count(session) == 3
    assert len(
        {
            legacy.content_event_id,
            bound_a.content_event_id,
            bound_b.content_event_id,
        }
    ) == 3
    assert len({legacy.event_id, bound_a.event_id, bound_b.event_id}) == 3
    assert legacy.content_digest == bound_a.content_digest
    assert bound_a.content_digest == bound_b.content_digest
    assert legacy.provider_binding_digest is None
    assert bound_a.provider_binding_digest == PROVIDER_BINDING_A
    assert bound_b.provider_binding_digest == PROVIDER_BINDING_B

    observation = session.get(WellnessEvent, bound_a.event_id)
    content = session.get(WellnessEvent, bound_a.content_event_id)
    assert observation is not None
    assert content is not None
    for event in (observation, content):
        assert event.payload["provider_binding_digest"] == (
            PROVIDER_BINDING_A
        )
        assert event.quality_flags["provider_binding_digest"] == (
            PROVIDER_BINDING_A
        )
        assert event.derived_from["provider_binding_digest"] == (
            PROVIDER_BINDING_A
        )
        assert PROVIDER_BINDING_A not in json.dumps(
            event.payload.get("normalized_context", {}),
            sort_keys=True,
        )

    assert wearable_snapshot_from_event(
        session,
        observation,
        now=NOW,
    ) == bound_a
    assert wearable_snapshot_from_event(
        session,
        observation,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) == bound_a
    assert wearable_snapshot_from_event(
        session,
        observation,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_B,
    ) is None

    legacy_event = session.get(WellnessEvent, legacy.event_id)
    assert legacy_event is not None
    assert wearable_snapshot_from_event(
        session,
        legacy_event,
        now=NOW,
    ) == legacy
    assert wearable_snapshot_from_event(
        session,
        legacy_event,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) is None
    assert latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) == bound_a
    assert latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_C,
    ) is None


def test_daily_snapshot_isolated_by_exact_execution_scope_digest(
    session,
) -> None:
    stress_scope = open_wearables_daily_execution_scope_digest(
        capability="wearable.stress",
        allowed_providers=("garmin",),
        provider_binding_digest=PROVIDER_BINDING_A,
    )
    recovery_scope = open_wearables_daily_execution_scope_digest(
        capability="wearable.recovery",
        allowed_providers=("garmin",),
        provider_binding_digest=PROVIDER_BINDING_A,
    )

    stress = persist_open_wearables_observation(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
        provider_binding_digest=PROVIDER_BINDING_A,
        execution_scope_digest=stress_scope,
    )
    recovery = persist_open_wearables_observation(
        session,
        normalized_context=_context(),
        local_day=DAY,
        timezone="UTC",
        collected_at=NOW,
        now=NOW,
        provider_binding_digest=PROVIDER_BINDING_A,
        execution_scope_digest=recovery_scope,
    )

    assert stress.content_digest == recovery.content_digest
    assert stress.content_event_id != recovery.content_event_id
    assert stress.event_id != recovery.event_id
    assert _count(session) == 2
    assert _observation_count(session) == 2
    assert latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
        expected_execution_scope_digest=stress_scope,
    ) == stress
    assert latest_retained_open_wearables_snapshot(
        session,
        local_day=DAY,
        timezone="UTC",
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
        expected_execution_scope_digest=recovery_scope,
    ) == recovery
    assert (
        latest_retained_open_wearables_snapshot(
            session,
            local_day=DAY,
            timezone="UTC",
            now=NOW,
            expected_provider_binding_digest=PROVIDER_BINDING_A,
            expected_execution_scope_digest="sha256:" + ("f" * 64),
        )
        is None
    )


def test_generic_query_provider_binding_is_private_and_identity_bound(
    session,
) -> None:
    start = datetime(2026, 8, 10, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    parameters = {
        "series_type": "heart_rate",
        "resolution": "1min",
    }
    result = {
        "status": "ok",
        "records": [
            {
                "timestamp": start.isoformat(),
                "series_type": "heart_rate",
                "value": 72,
                "unit": "bpm",
            }
        ],
        "limitations": [],
    }

    legacy = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters=parameters,
        result=result,
        collected_at=NOW,
        now=NOW,
    )
    bound_a = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters=parameters,
        result=result,
        collected_at=NOW,
        now=NOW,
        provider_binding_digest=PROVIDER_BINDING_A,
    )
    bound_b = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters=parameters,
        result=result,
        collected_at=NOW,
        now=NOW,
        provider_binding_digest=PROVIDER_BINDING_B,
    )

    assert _query_count(session) == 3
    assert len({legacy.event_id, bound_a.event_id, bound_b.event_id}) == 3
    assert legacy.query_digest == bound_a.query_digest
    assert bound_a.query_digest == bound_b.query_digest
    assert legacy.provider_binding_digest is None
    assert bound_a.provider_binding_digest == PROVIDER_BINDING_A
    assert bound_b.provider_binding_digest == PROVIDER_BINDING_B

    event = session.get(WellnessEvent, bound_a.event_id)
    assert event is not None
    assert event.payload["provider_binding_digest"] == PROVIDER_BINDING_A
    assert event.quality_flags["provider_binding_digest"] == (
        PROVIDER_BINDING_A
    )
    assert event.derived_from["provider_binding_digest"] == (
        PROVIDER_BINDING_A
    )
    assert "provider_binding_digest" not in event.payload["query"]
    assert PROVIDER_BINDING_A not in json.dumps(
        event.payload["result"],
        sort_keys=True,
    )
    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    ) == bound_a
    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) == bound_a
    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_B,
    ) is None

    legacy_event = session.get(WellnessEvent, legacy.event_id)
    assert legacy_event is not None
    assert wearable_query_snapshot_from_event(
        session,
        legacy_event,
        now=NOW,
    ) == legacy
    assert wearable_query_snapshot_from_event(
        session,
        legacy_event,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) is None
    assert latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters=parameters,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) == bound_a
    assert latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.timeseries",
        start=start,
        end=end,
        timezone="UTC",
        parameters=parameters,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_C,
    ) is None


def test_whoop_binding_digest_is_independent_from_private_provenance(
    session,
) -> None:
    first = _persist_whoop_package(
        session,
        provider_binding_digest=PROVIDER_BINDING_A,
    )
    second = _persist_whoop_package(
        session,
        provider_binding_digest=PROVIDER_BINDING_B,
    )

    assert first.event_id != second.event_id
    assert first.query_digest == second.query_digest
    assert (
        first.private_provenance_digest
        == second.private_provenance_digest
    )
    assert first.provider_binding_digest == PROVIDER_BINDING_A
    assert second.provider_binding_digest == PROVIDER_BINDING_B
    assert (
        first.provider_binding_digest
        != first.private_provenance_digest
    )

    event = session.get(WellnessEvent, first.event_id)
    assert event is not None
    assert event.payload["provider_binding_digest"] == PROVIDER_BINDING_A
    assert event.payload["private_provenance_digest"] == (
        first.private_provenance_digest
    )
    assert event.quality_flags["provider_binding_digest"] == (
        PROVIDER_BINDING_A
    )
    assert event.derived_from["provider_binding_digest"] == (
        PROVIDER_BINDING_A
    )
    assert "provider_binding_digest" not in event.payload["query"]
    assert PROVIDER_BINDING_A not in json.dumps(
        first.result,
        sort_keys=True,
    )
    assert PROVIDER_BINDING_A not in json.dumps(
        event.payload["private_provenance"],
        sort_keys=True,
    )
    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) == first
    assert latest_retained_open_wearables_query_snapshot(
        session,
        capability="wearable.whoop-recovery-package",
        start=WHOOP_START,
        end=WHOOP_END,
        timezone="UTC",
        parameters=WHOOP_PARAMETERS,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) == first


@pytest.mark.parametrize(
    "tamper",
    (
        "payload",
        "quality_flags",
        "derived_from",
        "source_record_id",
    ),
)
def test_bound_query_rejects_provider_binding_tampering(
    session,
    tamper: str,
) -> None:
    snapshot = _persist_whoop_package(
        session,
        provider_binding_digest=PROVIDER_BINDING_A,
    )
    event = session.get(WellnessEvent, snapshot.event_id)
    assert event is not None

    if tamper == "payload":
        event.payload = {
            **event.payload,
            "provider_binding_digest": PROVIDER_BINDING_B,
        }
    elif tamper == "quality_flags":
        event.quality_flags = {
            **event.quality_flags,
            "provider_binding_digest": PROVIDER_BINDING_B,
        }
    elif tamper == "derived_from":
        event.derived_from = {
            **event.derived_from,
            "provider_binding_digest": PROVIDER_BINDING_B,
        }
    else:
        event.source_record_id = f"{event.source_record_id}-tampered"
    session.flush([event])

    assert wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
        expected_provider_binding_digest=PROVIDER_BINDING_A,
    ) is None
