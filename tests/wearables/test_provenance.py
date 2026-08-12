import math
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from healthmes.storage.service import update_retention_policy
from healthmes.store import WellnessEvent
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
    commit_open_wearables_snapshot,
    latest_retained_open_wearables_snapshot,
    persist_open_wearables_observation,
    persist_open_wearables_snapshot,
)

DAY = date(2026, 8, 10)
NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)


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
    update_retention_policy(session, "normalized", "1d", now=NOW)
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
    update_retention_policy(session, "normalized", "1d", now=NOW)
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
    update_retention_policy(session, "normalized", "forever", now=NOW)
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
