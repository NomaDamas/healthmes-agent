"""Tests for the medical-record router (local viewing only, no writes)."""

from datetime import UTC, datetime

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


def test_rest_surface_is_read_only(client):
    """Writes happen only through the create_medical_record MCP tool."""
    response = client.post("/v1/medical-records", json={"kind": "medication", "description": "x"})
    assert response.status_code == 405


def test_list_rejects_unknown_kind(client):
    response = client.get("/v1/medical-records", params={"kind": "allergy"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
