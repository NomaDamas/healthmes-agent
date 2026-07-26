"""Hourly persist job, scheduler-hook integration, and the OW reader.

Time control uses freezegun (fake clock); the store is in-memory sqlite; the
open-wearables boundary is either the async fake reader or the real
``OwEnergyReader`` over an ``httpx.MockTransport``.
"""

import datetime as dt

import httpx
import pytest
from freezegun import freeze_time
from sqlalchemy import select

from healthmes.config import Settings
from healthmes.engine.cognitive_energy import (
    STATUS_INSUFFICIENT,
    STATUS_OK,
    CognitiveEnergyEngine,
    OwEnergyReader,
    OwRows,
    build_energy_job,
)
from healthmes.engine.scheduler import ENERGY_JOB_ID, create_scheduler, register_energy_job
from healthmes.store import CognitiveEnergyEstimate

UTC = dt.UTC


def _stored_estimates(session_factory) -> list[CognitiveEnergyEstimate]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(CognitiveEnergyEstimate).order_by(CognitiveEnergyEstimate.window_start)
            )
        )


# ---------------------------------------------------------------------------
# Hourly persist (fake clock via freezegun)
# ---------------------------------------------------------------------------


class TestPersistCurrentWindow:
    @freeze_time("2026-07-09 14:23:00")
    def test_writes_row_for_the_current_hour(
        self, energy_engine_factory, session_factory, full_signal_ow_rows, seed_vector_store
    ) -> None:
        seed_vector_store()
        engine = energy_engine_factory(full_signal_ow_rows)

        estimate = engine.persist_current_window()

        assert estimate.status == STATUS_OK
        assert estimate.score == 65  # the hand vector of test_cognitive_energy.py
        assert estimate.score_exact == pytest.approx(64.75)

        rows = _stored_estimates(session_factory)
        assert len(rows) == 1
        row = rows[0]
        # sqlite returns naive datetimes; they are UTC by convention.
        assert row.window_start.replace(tzinfo=UTC) == dt.datetime(2026, 7, 9, 14, 0, tzinfo=UTC)
        assert row.window_end.replace(tzinfo=UTC) == dt.datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        assert row.score == 65

    @freeze_time("2026-07-09 14:23:00")
    def test_persisted_components_carry_all_factors_and_sum_to_score(
        self, energy_engine_factory, session_factory, full_signal_ow_rows, seed_vector_store
    ) -> None:
        seed_vector_store()
        engine = energy_engine_factory(full_signal_ow_rows)
        engine.persist_current_window()

        (row,) = _stored_estimates(session_factory)
        payload = row.components
        assert payload["version"] == 1
        items = payload["items"]
        assert [item["name"] for item in items] == [
            "base",
            "sleep_debt_penalty",
            "stress_penalty",
            "hrv_deviation_penalty",
            "body_battery_bonus",
            "meeting_load_penalty",
            "fragmentation_penalty",
        ]
        total = sum(item["contribution"] for item in items)
        assert total == pytest.approx(payload["score_exact"], abs=1e-9)
        assert abs(row.score - payload["score_exact"]) <= 0.5
        assert row.inputs_snapshot["missing_signals"] == []
        assert row.inputs_snapshot["ow"]["status"] == "ok"

    def test_rerun_same_hour_upserts_single_row(
        self, energy_engine_factory, session_factory, full_signal_ow_rows, seed_vector_store
    ) -> None:
        seed_vector_store()
        engine = energy_engine_factory(full_signal_ow_rows)
        with freeze_time("2026-07-09 14:23:00"):
            engine.persist_current_window()
        with freeze_time("2026-07-09 14:55:00"):
            engine.persist_current_window()

        rows = _stored_estimates(session_factory)
        assert len(rows) == 1
        assert rows[0].score == 65

    def test_next_hour_appends_a_second_row(
        self, energy_engine_factory, session_factory, full_signal_ow_rows, seed_vector_store
    ) -> None:
        seed_vector_store()
        engine = energy_engine_factory(full_signal_ow_rows)
        with freeze_time("2026-07-09 14:23:00"):
            engine.persist_current_window()
        with freeze_time("2026-07-09 15:05:00"):
            engine.persist_current_window()

        rows = _stored_estimates(session_factory)
        assert [row.window_start.replace(tzinfo=UTC) for row in rows] == [
            dt.datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
            dt.datetime(2026, 7, 9, 15, 0, tzinfo=UTC),
        ]

    @freeze_time("2026-07-09 14:23:00")
    def test_insufficient_data_is_not_persisted(
        self, energy_engine_factory, session_factory
    ) -> None:
        engine = energy_engine_factory(OwRows())  # OW empty, store empty
        estimate = engine.persist_current_window()
        assert estimate.status == STATUS_INSUFFICIENT
        assert estimate.score is None
        assert _stored_estimates(session_factory) == []

    @freeze_time("2026-07-09 14:23:00")
    def test_ow_unavailable_drops_health_factors_with_reason(
        self, energy_engine_factory, seed_vector_store
    ) -> None:
        seed_vector_store()
        engine = energy_engine_factory(OwRows(status="unavailable", detail="boom"))
        estimate = engine.compute_window()
        assert estimate.status == STATUS_OK  # store factors still present
        names = {item["name"] for item in estimate.components}
        assert names == {"base", "meeting_load_penalty", "fragmentation_penalty"}
        reasons = {
            item["name"]: item["reason"]
            for item in estimate.inputs_snapshot["missing_signals"]
        }
        assert reasons == {
            "sleep_debt": "ow_unavailable",
            "stress": "ow_unavailable",
            "hrv_deviation": "ow_unavailable",
            "body_battery": "ow_unavailable",
        }
        assert estimate.inputs_snapshot["ow"]["detail"] == "boom"


# ---------------------------------------------------------------------------
# Job wrapper + scheduler hook (the hook the triggers agent left)
# ---------------------------------------------------------------------------


class TestBuildEnergyJob:
    @freeze_time("2026-07-09 14:23:00")
    def test_job_persists_via_engine(
        self, settings, energy_engine_factory, session_factory, full_signal_ow_rows,
        seed_vector_store,
    ) -> None:
        seed_vector_store()
        factory_calls: list[int] = []

        def engine_factory() -> CognitiveEnergyEngine:
            factory_calls.append(1)
            return energy_engine_factory(full_signal_ow_rows)

        job = build_energy_job(settings, engine_factory=engine_factory)
        job()
        job()  # engine is constructed once and reused

        assert len(factory_calls) == 1
        assert len(_stored_estimates(session_factory)) == 1

    def test_job_contains_exceptions(self, settings: Settings) -> None:
        def exploding_factory() -> CognitiveEnergyEngine:
            raise RuntimeError("boom")

        job = build_energy_job(settings, engine_factory=exploding_factory)
        job()  # must not raise (scheduler jobs may never take the loop down)

    def test_job_registers_on_the_scheduler_hook(self, settings: Settings) -> None:
        scheduler = create_scheduler(settings, trigger_job=lambda: None)
        try:
            job = build_energy_job(settings, engine_factory=lambda: None)  # type: ignore[arg-type]
            registered = register_energy_job(scheduler, job)
            assert scheduler.get_job(ENERGY_JOB_ID) is registered
            assert registered.func is job
        finally:
            if scheduler.running:  # pragma: no cover - defensive
                scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# OwEnergyReader over httpx.MockTransport (no network)
# ---------------------------------------------------------------------------


def _make_ow_client(handler) -> object:
    from healthmes.mcp_server.ow_client import OWClient

    return OWClient(
        base_url="http://open-wearables.test",
        api_key="test-ow-api-key",
        transport=httpx.MockTransport(handler),
    )


def _empty_page() -> dict:
    return {"data": [], "pagination": {"next_cursor": None, "has_more": False}}


class TestOwEnergyReader:
    def test_reads_scores_and_sleep_summaries(self, settings, make_score_row) -> None:
        seen: list[str] = []
        score_row = make_score_row("stress", "garmin", "2026-07-09T10:00:00+00:00", 40)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/api/v1/users":
                return httpx.Response(
                    200, json={"items": [{"id": "user-1"}], "total": 1, "page": 1, "limit": 2}
                )
            if request.url.path.endswith("/health-scores"):
                assert request.url.params["start_date"] == "2026-06-18"  # 21-day history
                assert request.url.params["end_date"] == "2026-07-10"
                return httpx.Response(
                    200,
                    json={
                        "data": [score_row],
                        "pagination": {"next_cursor": None, "has_more": False},
                    },
                )
            if request.url.path.endswith("/summaries/sleep"):
                return httpx.Response(
                    200,
                    json={
                        "data": [{"date": "2026-07-09", "avg_hrv_rmssd_ms": 45.0}],
                        "pagination": {"next_cursor": None, "has_more": False},
                    },
                )
            return httpx.Response(404, json={"detail": "not found"})

        reader = OwEnergyReader(settings, client=_make_ow_client(handler))
        rows = _run(reader.read(dt.date(2026, 7, 9)))

        assert rows.status == "ok"
        assert rows.score_rows == (score_row,)
        assert rows.sleep_rows[0]["date"] == "2026-07-09"
        assert "/api/v1/users" in seen
        assert "/api/v1/users/user-1/health-scores" in seen

    def test_configured_user_id_skips_discovery(self, settings) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json=_empty_page())

        pinned = settings.model_copy(update={"ow_user_id": "pinned-user"})
        reader = OwEnergyReader(pinned, client=_make_ow_client(handler))
        rows = _run(reader.read(dt.date(2026, 7, 9)))

        assert rows.status == "ok"
        assert "/api/v1/users" not in seen
        assert "/api/v1/users/pinned-user/health-scores" in seen

    def test_backend_failure_degrades_to_unavailable(self, settings) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "kaboom"})

        reader = OwEnergyReader(settings, client=_make_ow_client(handler))
        rows = _run(reader.read(dt.date(2026, 7, 9)))

        assert rows.status == "unavailable"
        assert rows.score_rows == ()
        assert rows.sleep_rows == ()
        assert rows.detail is not None

    def test_no_users_degrades_to_unavailable(self, settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/users":
                return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "limit": 2})
            return httpx.Response(200, json=_empty_page())

        reader = OwEnergyReader(settings, client=_make_ow_client(handler))
        rows = _run(reader.read(dt.date(2026, 7, 9)))
        assert rows.status == "unavailable"

    def test_multiple_users_without_pin_degrade_instead_of_guessing(self, settings) -> None:
        """Two visible users + no configured id: never silently read users[0]
        (that would write someone else's scores into the shared energy
        history) — the shared resolver requires exactly one."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/api/v1/users":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"id": "user-1"}, {"id": "partner-2"}],
                        "total": 2,
                        "page": 1,
                        "limit": 2,
                    },
                )
            return httpx.Response(200, json=_empty_page())

        reader = OwEnergyReader(settings, client=_make_ow_client(handler))
        rows = _run(reader.read(dt.date(2026, 7, 9)))

        assert rows.status == "unavailable"
        assert rows.detail is not None and "HEALTHMES_OW_USER_ID" in rows.detail
        assert not any(path.endswith("/health-scores") for path in seen)


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
