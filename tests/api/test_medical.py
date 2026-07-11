"""Tests for the medical-record router: viewing + the issue #10 native capture.

``POST /v1/medical-records`` is the REST twin of the ``create_medical_record``
MCP tool (the Telegram capture path): it must attach the same server-computed
health-context snapshot under the record's ``health`` context key and must
never fail a capture because open-wearables is unreachable.
"""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from healthmes.mcp_server import server as server_module
from healthmes.mcp_server.ow_client import OWClient
from healthmes.store import MedicalRecord, MedicalRecordKind


def _seed(session, *rows: MedicalRecord) -> list[MedicalRecord]:
    for row in rows:
        session.add(row)
    session.commit()
    for row in rows:
        session.refresh(row)
    return list(rows)


def _record(
    description: str,
    *,
    kind: MedicalRecordKind = MedicalRecordKind.MEDICATION,
    created_at: datetime | None = None,
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
    return row


def test_list_is_newest_first_with_kind_filter(client, session):
    _seed(
        session,
        _record("Older med", created_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC)),
        _record("Newer med", created_at=datetime(2026, 7, 8, 9, 0, tzinfo=UTC)),
        _record(
            "Rash on forearm",
            kind=MedicalRecordKind.SYMPTOM,
            created_at=datetime(2026, 7, 5, 9, 0, tzinfo=UTC),
        ),
    )

    response = client.get("/v1/medical-records")
    assert response.status_code == 200
    body = response.json()
    assert [r["description"] for r in body["data"]] == [
        "Newer med",
        "Rash on forearm",
        "Older med",
    ]
    assert body["pagination"]["total_count"] == 3

    only_symptoms = client.get("/v1/medical-records", params={"kind": "symptom"}).json()
    assert [r["description"] for r in only_symptoms["data"]] == ["Rash on forearm"]
    assert only_symptoms["data"][0]["kind"] == "symptom"


def test_list_created_at_range_filter(client, session):
    for day, description in ((1, "Old"), (5, "Mid"), (9, "New")):
        _seed(
            session,
            _record(description, created_at=datetime(2026, 7, day, 12, 0, tzinfo=UTC)),
        )

    response = client.get(
        "/v1/medical-records",
        params={"start": "2026-07-02T00:00:00Z", "end": "2026-07-09T00:00:00Z"},
    )

    body = response.json()
    assert [r["description"] for r in body["data"]] == ["Mid"]
    assert body["pagination"]["total_count"] == 1


def test_list_omits_context_but_keeps_local_fields(client, session):
    _seed(
        session,
        _record(
            "Ibuprofen 400mg",
            media_path="media/2026/07/09/pills.jpg",
            transcript="took ibuprofen after lunch",
            context={"health": {"status": "ok"}},
        ),
    )

    entry = client.get("/v1/medical-records").json()["data"][0]
    assert entry["media_path"] == "media/2026/07/09/pills.jpg"
    assert entry["transcript"] == "took ibuprofen after lunch"
    assert "context" not in entry  # detail-only: the snapshot can be large


def test_get_returns_full_context_snapshot(client, session):
    context = {
        "health": {"status": "ok", "confidence": "medium", "sleep_debt": {"status": "ok"}},
        "capture": {"source": "telegram-photo"},
    }
    (row,) = _seed(
        session,
        _record(
            "Rash, ~2cm, left forearm",
            kind=MedicalRecordKind.SYMPTOM,
            context=context,
        ),
    )

    response = client.get(f"/v1/medical-records/{row.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(row.id)
    assert body["kind"] == "symptom"
    assert body["context"] == context


def test_get_unknown_record_returns_error_envelope(client):
    response = client.get("/v1/medical-records/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_list_rejects_unknown_kind(client):
    response = client.get("/v1/medical-records", params={"kind": "allergy"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# POST /v1/medical-records — the issue #10 native capture write path
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_snapshot(monkeypatch):
    """Pin the server-side health snapshot (no open-wearables round trip)."""
    stub = {"status": "ok", "confidence": "high", "stubbed": True}

    async def _snapshot() -> dict:
        return dict(stub)

    # The REST handler resolves the helper through the module object at call
    # time, so patching the mcp_server attribute covers both write paths.
    monkeypatch.setattr(server_module, "_capture_health_context", _snapshot)
    return stub


def test_create_accepts_uploaded_media_path_round_trip(client, session, stub_snapshot):
    uploaded = client.post(
        "/v1/media", files={"file": ("pills.heic", b"heic-bytes" * 8, "image/heic")}
    )
    assert uploaded.status_code == 201
    media_path = uploaded.json()["media_path"]

    response = client.post(
        "/v1/medical-records",
        json={
            "kind": "medication",
            "description": "Tylenol 500mg, 2 tablets after lunch",
            "media_path": media_path,
            "context": {"source": "ios-app-photo"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "medication"
    assert body["media_path"] == media_path
    # Server-attached snapshot + caller capture metadata, MCP-tool layout.
    assert body["context"]["health"] == stub_snapshot
    assert body["context"]["capture"] == {"source": "ios-app-photo"}

    row = session.scalars(select(MedicalRecord)).one()
    assert str(row.id) == body["id"]
    assert row.media_path == media_path

    # The stored path serves back through the media route (capture loop).
    fetched = client.get(f"/v1/media/{media_path}")
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/heic"


def test_create_without_context_stores_snapshot_only(client, stub_snapshot):
    response = client.post(
        "/v1/medical-records",
        json={"kind": "symptom", "description": "Red rash on left forearm"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["media_path"] is None
    assert body["transcript"] is None
    assert set(body["context"]) == {"health"}


def test_create_survives_unreachable_open_wearables(client):
    """The capture is the priority: OW down degrades the snapshot only."""

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    server_module.set_ow_client(
        OWClient(
            base_url="http://open-wearables.test",
            api_key="test-key",
            transport=httpx.MockTransport(_refuse),
        )
    )
    server_module.set_ow_user_id("7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e")
    try:
        response = client.post(
            "/v1/medical-records",
            json={"kind": "symptom", "description": "Rash, ~2cm, left forearm"},
        )
    finally:
        server_module.set_ow_client(None)
        server_module.set_ow_user_id(None)

    assert response.status_code == 201
    health = response.json()["context"]["health"]
    assert health["status"] == "unavailable"
    assert "open-wearables" in health["reason"]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "allergy", "description": "x"},  # unknown kind
        {"kind": "medication", "description": ""},  # empty description
        {"kind": "medication", "description": "   "},  # blank description
        {"kind": "medication"},  # missing description
    ],
)
def test_create_rejects_invalid_bodies(client, body):
    response = client.post("/v1/medical-records", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
