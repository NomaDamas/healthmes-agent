"""Tests for the medical-lite capture MCP tools (docs/PLAN.md §8, Phase 3).

``create_medical_record`` must persist locally with a deterministic
capture-time health snapshot (reusing the readiness-context helper) and never
lose a capture because open-wearables is unreachable. ``list_medical_records``
must honor the privacy contract: descriptions yes, transcripts never.
"""

import datetime as dt
import uuid

import httpx
import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthmes.mcp_server import server as server_module
from healthmes.mcp_server.ow_client import OWClient
from healthmes.store import MedicalRecord, MedicalRecordKind


def _seed_ok_charge(fake_ow) -> None:
    """One fresh body_battery score so the readiness charge block is ok."""
    fake_ow.add_score(
        "body_battery",
        "garmin",
        dt.datetime.now(dt.UTC).isoformat(),
        62.0,
        qualifier="medium",
    )


def _add_record(
    session,
    *,
    kind: MedicalRecordKind = MedicalRecordKind.MEDICATION,
    description: str = "Tylenol 500mg, 2 tablets",
    created_at: dt.datetime | None = None,
    media_path: str | None = None,
    transcript: str | None = None,
    context: dict | None = None,
) -> MedicalRecord:
    row = MedicalRecord(
        kind=kind,
        description=description,
        media_path=media_path,
        transcript=transcript,
        context=context,
    )
    if created_at is not None:
        row.created_at = created_at
        row.updated_at = created_at
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


class TestCreateMedicalRecord:
    async def test_create_persists_record_with_health_snapshot(
        self, mcp_client, call_tool, mcp_env, store_factory
    ):
        _seed_ok_charge(mcp_env)

        result = await call_tool(
            mcp_client,
            "create_medical_record",
            {
                "kind": "medication",
                "description": "Tylenol 500mg, 2 tablets after lunch",
                "media_path": "media/2026/07/09/pills.jpg",
                "context": {"source": "telegram-photo"},
            },
        )

        assert result["status"] == "ok"
        assert result["created"] is True
        assert result["kind"] == "medication"
        assert result["health_context_status"] == "ok"
        assert result["recorded_at"]  # ISO timestamp of the stored row

        with store_factory() as session:
            row = session.scalars(select(MedicalRecord)).one()
        assert str(row.id) == result["medical_record_id"]
        assert row.kind is MedicalRecordKind.MEDICATION
        assert row.description == "Tylenol 500mg, 2 tablets after lunch"
        assert row.media_path == "media/2026/07/09/pills.jpg"
        assert row.transcript is None
        # Deterministic snapshot: the readiness-context block set, not LLM data.
        health = row.context["health"]
        assert health["status"] == "ok"
        assert {"sleep_debt", "hrv", "stress", "charge", "yesterday_load"} <= set(health)
        assert health["charge"]["entries"][0]["category"] == "body_battery"
        assert row.context["capture"] == {"source": "telegram-photo"}

    async def test_create_snapshot_is_honest_when_data_is_thin(
        self, mcp_client, call_tool, store_factory
    ):
        """No wearable data at all -> the snapshot says insufficient_data."""
        result = await call_tool(
            mcp_client,
            "create_medical_record",
            {"kind": "symptom", "description": "Red rash on left forearm, since morning"},
        )

        assert result["created"] is True
        assert result["health_context_status"] == "insufficient_data"
        with store_factory() as session:
            row = session.scalars(select(MedicalRecord)).one()
        assert row.kind is MedicalRecordKind.SYMPTOM
        assert row.context["health"]["status"] == "insufficient_data"

    async def test_capture_survives_unreachable_open_wearables(
        self, mcp_client, call_tool, store_factory
    ):
        """The capture is the priority: OW down degrades the snapshot only."""

        def _refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        server_module.set_ow_client(
            OWClient(
                base_url="http://ow.unreachable.test",
                api_key="irrelevant",
                transport=httpx.MockTransport(_refuse),
            )
        )

        result = await call_tool(
            mcp_client,
            "create_medical_record",
            {"kind": "medication", "description": "Ibuprofen 400mg, 1 tablet"},
        )

        assert result["created"] is True
        assert result["health_context_status"] == "unavailable"
        with store_factory() as session:
            row = session.scalars(select(MedicalRecord)).one()
        health = row.context["health"]
        assert health["status"] == "unavailable"
        assert "connection refused" in health["reason"]

    async def test_transcript_is_stored_for_voice_captures(
        self, mcp_client, call_tool, store_factory
    ):
        await call_tool(
            mcp_client,
            "create_medical_record",
            {
                "kind": "symptom",
                "description": "Pounding headache since lunch, severity 6/10 (stated)",
                "transcript": "my head has been pounding since lunch, like a six out of ten",
            },
        )
        with store_factory() as session:
            row = session.scalars(select(MedicalRecord)).one()
        assert row.transcript.startswith("my head has been pounding")

    async def test_rejects_unknown_kind_and_empty_description(self, mcp_client, store_factory):
        with pytest.raises(ToolError, match="kind"):
            await mcp_client.call_tool(
                "create_medical_record", {"kind": "vaccine", "description": "Flu shot"}
            )
        with pytest.raises(ToolError, match="description"):
            await mcp_client.call_tool(
                "create_medical_record", {"kind": "symptom", "description": "   "}
            )
        with store_factory() as session:
            assert session.scalars(select(MedicalRecord)).all() == []


class TestOneTapCorrection:
    async def test_correction_preserves_media_transcript_and_snapshot(
        self, mcp_client, call_tool, mcp_env, store_factory
    ):
        _seed_ok_charge(mcp_env)
        created = await call_tool(
            mcp_client,
            "create_medical_record",
            {
                "kind": "symptom",
                "description": "White pills in a blister pack",
                "media_path": "media/2026/07/09/blister.jpg",
                "transcript": "logging these pills",
                "context": {"source": "telegram-photo"},
            },
        )
        record_id = created["medical_record_id"]

        corrected = await call_tool(
            mcp_client,
            "create_medical_record",
            {
                "record_id": record_id,
                "kind": "medication",
                "description": "Magnesium 250mg supplement, 1 tablet daily",
            },
        )

        assert corrected["created"] is False
        assert corrected["medical_record_id"] == record_id
        assert corrected["kind"] == "medication"
        # The capture-time snapshot is preserved, not re-fetched or dropped.
        assert corrected["health_context_status"] == "ok"

        with store_factory() as session:
            row = session.scalars(select(MedicalRecord)).one()
        assert row.kind is MedicalRecordKind.MEDICATION
        assert row.description == "Magnesium 250mg supplement, 1 tablet daily"
        assert row.media_path == "media/2026/07/09/blister.jpg"  # never lost
        assert row.transcript == "logging these pills"
        assert row.context["health"]["status"] == "ok"
        assert row.context["capture"] == {"source": "telegram-photo"}

    async def test_correction_can_replace_capture_context_but_not_health(
        self, mcp_client, call_tool, store_factory
    ):
        created = await call_tool(
            mcp_client,
            "create_medical_record",
            {
                "kind": "medication",
                "description": "Aspirin 100mg",
                "context": {"source": "telegram-photo"},
            },
        )
        await call_tool(
            mcp_client,
            "create_medical_record",
            {
                "record_id": created["medical_record_id"],
                "kind": "medication",
                "description": "Aspirin 100mg, taken daily (user corrected)",
                "context": {"source": "telegram-photo", "user_stated_time": "daily"},
            },
        )
        with store_factory() as session:
            row = session.scalars(select(MedicalRecord)).one()
        assert row.context["capture"]["user_stated_time"] == "daily"
        assert row.context["health"]["status"] == "insufficient_data"  # untouched

    async def test_correction_of_unknown_record_fails_loudly(self, mcp_client):
        with pytest.raises(ToolError, match="not found"):
            await mcp_client.call_tool(
                "create_medical_record",
                {
                    "record_id": str(uuid.uuid4()),
                    "kind": "medication",
                    "description": "Anything",
                },
            )


class TestListMedicalRecords:
    async def test_list_is_oldest_first_and_never_leaks_transcripts(
        self, mcp_client, call_tool, store_factory, tmp_path
    ):
        now = dt.datetime.now(dt.UTC)
        with store_factory() as session:
            _add_record(
                session,
                description="Older medication",
                created_at=now - dt.timedelta(days=10),
                transcript="raw voice transcript that must stay local",
            )
            _add_record(
                session,
                kind=MedicalRecordKind.SYMPTOM,
                description="Newer symptom",
                created_at=now - dt.timedelta(days=1),
                media_path="media/2026/07/08/rash.jpg",
                context={"health": {"status": "ok"}},
            )

        result = await call_tool(mcp_client, "list_medical_records", {})

        assert result["status"] == "ok"
        assert result["kind"] == "all"
        assert result["count"] == 2
        assert result["truncated"] is False
        descriptions = [record["description"] for record in result["records"]]
        assert descriptions == ["Older medication", "Newer symptom"]  # timeline order

        older, newer = result["records"]
        assert older["has_transcript"] is True
        assert "transcript" not in older  # privacy: text never re-enters the LLM
        assert "context" not in older  # not requested
        assert newer["media_path"] == "media/2026/07/08/rash.jpg"
        assert newer["health_context_status"] == "ok"
        assert older["health_context_status"] is None

        # data_dir lets the briefing skill resolve relative media paths.
        assert result["data_dir"] == str((tmp_path / "data").resolve())
        assert result["window"]["days"] == 90

    async def test_list_filters_by_kind_and_trailing_window(
        self, mcp_client, call_tool, store_factory
    ):
        now = dt.datetime.now(dt.UTC)
        with store_factory() as session:
            _add_record(session, description="Too old", created_at=now - dt.timedelta(days=40))
            _add_record(session, description="Recent med", created_at=now - dt.timedelta(days=2))
            _add_record(
                session,
                kind=MedicalRecordKind.SYMPTOM,
                description="Recent symptom",
                created_at=now - dt.timedelta(days=2),
            )

        result = await call_tool(
            mcp_client, "list_medical_records", {"kind": "medication", "range": "30d"}
        )

        assert result["kind"] == "medication"
        assert [r["description"] for r in result["records"]] == ["Recent med"]

    async def test_include_context_returns_stored_snapshot(
        self, mcp_client, call_tool, store_factory
    ):
        with store_factory() as session:
            _add_record(
                session,
                kind=MedicalRecordKind.SYMPTOM,
                description="Rash",
                context={
                    "health": {"status": "ok", "confidence": "medium"},
                    "capture": {"source": "telegram-photo"},
                },
            )

        result = await call_tool(mcp_client, "list_medical_records", {"include_context": True})

        record = result["records"][0]
        assert record["context"]["health"]["confidence"] == "medium"
        assert record["context"]["capture"] == {"source": "telegram-photo"}

    async def test_limit_keeps_the_most_recent_records(self, mcp_client, call_tool, store_factory):
        now = dt.datetime.now(dt.UTC)
        with store_factory() as session:
            for days_ago in (3, 2, 1):
                _add_record(
                    session,
                    description=f"med-{days_ago}",
                    created_at=now - dt.timedelta(days=days_ago),
                )

        result = await call_tool(mcp_client, "list_medical_records", {"limit": 2})

        assert result["truncated"] is True
        assert [r["description"] for r in result["records"]] == ["med-2", "med-1"]

    async def test_rejects_bad_kind_range_and_limit(self, mcp_client):
        with pytest.raises(ToolError, match="kind"):
            await mcp_client.call_tool("list_medical_records", {"kind": "allergy"})
        with pytest.raises(ToolError, match="range"):
            await mcp_client.call_tool("list_medical_records", {"range": "500d"})
        with pytest.raises(ToolError, match="limit"):
            await mcp_client.call_tool("list_medical_records", {"limit": 0})
