"""Tests for the decision viewer routes (JSON + placeholder HTML page)."""

import uuid
from datetime import UTC, datetime, timedelta

from freezegun import freeze_time

from healthmes.store import DecisionKind, DecisionRecord

TREE = {
    "id": "root",
    "type": "rule",
    "label": "stress_spike rule fired",
    "detail": "stress 82 vs baseline 55",
    "children": [
        {
            "id": "n1",
            "type": "input",
            "label": "afternoon calendar load <b>3h</b>",
            "children": [],
        },
        {
            "id": "n2",
            "type": "action",
            "label": "proposed moving focus block",
            "children": [],
        },
    ],
}


def _seed_decision(session, summary: str = "Moved focus block to tomorrow") -> DecisionRecord:
    record = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        tree=TREE,
        summary=summary,
        llm_model="claude-test-1",
        tokens=321,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def test_get_decision_json(client, session):
    record = _seed_decision(session)

    response = client.get(f"/v1/decisions/{record.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(record.id)
    assert body["kind"] == "schedule_change"
    assert body["summary"] == "Moved focus block to tomorrow"
    assert body["tree"] == TREE
    assert body["llm_model"] == "claude-test-1"
    assert body["tokens"] == 321


def test_get_decision_json_404_envelope(client):
    response = client.get("/v1/decisions/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_private_decision_payload_is_not_exposed_by_json_or_html(
    client,
    session,
):
    private_marker = "private-question-and-query-trace"
    record = DecisionRecord(
        kind=DecisionKind.INSIGHT,
        tree={
            "id": "root",
            "type": "llm_step",
            "label": "Safe public answer",
            "children": [],
        },
        summary="Safe public answer",
        decision_request_id=uuid.uuid4(),
        decision_turn_id=uuid.uuid4(),
        decision_request_fingerprint="f" * 64,
        decision_payload={
            "schema": "healthmes.decision-private.v1",
            "request": {"question": private_marker},
            "source_refs": [
                {
                    "record_id": private_marker,
                    "reference_id": "sr_" + "0" * 32,
                }
            ],
            "tool_trace": [
                {"query": {"parameters": {"secret": private_marker}}}
            ],
        },
        decision_payload_digest="d" * 64,
    )
    session.add(record)
    session.commit()

    api_response = client.get(f"/v1/decisions/{record.id}")
    adjacent_response = client.get(f"/decisions/{record.id}.json")
    html_response = client.get(f"/decisions/{record.id}")

    assert api_response.status_code == 200
    assert adjacent_response.status_code == 200
    assert html_response.status_code == 200
    for response in (api_response, adjacent_response):
        body = response.json()
        assert "decision_payload" not in body
        assert private_marker not in response.text
    assert private_marker not in html_response.text
    assert "Safe public answer" in html_response.text


def test_decision_html_page_renders_tree_escaped(client, session):
    record = _seed_decision(session, summary="Summary with <script>alert(1)</script>")

    response = client.get(f"/decisions/{record.id}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "stress_spike rule fired" in html
    assert "proposed moving focus block" in html
    # User content is escaped, never raw markup.
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;3h&lt;/b&gt;" in html
    assert 'id="decision-tree"' in html


def test_decision_html_page_404_is_html(client):
    response = client.get("/decisions/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "해당 결정 기록이 없습니다" in response.text
    assert "00000000-0000-0000-0000-000000000000" in response.text


def test_decision_html_invalid_uuid_is_validation_error(client):
    response = client.get("/decisions/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_decision_read_surfaces_hide_exact_cutoff(
    client,
    session,
):
    assert client.get("/v1/decisions").status_code == 200
    with freeze_time("2026-08-16 12:00:00"):
        current = datetime.now(UTC)
        expired = _seed_decision(session, "exact-cutoff decision")
        available = _seed_decision(session, "available decision")
        expired.expires_at = current
        available.expires_at = current + timedelta(microseconds=1)
        session.commit()

        api_list = client.get("/v1/decisions")
        html_list = client.get("/decisions")
        api_detail = client.get(f"/v1/decisions/{expired.id}")
        adjacent_detail = client.get(
            f"/decisions/{expired.id}.json"
        )
        html_detail = client.get(f"/decisions/{expired.id}")
        available_detail = client.get(
            f"/v1/decisions/{available.id}"
        )

    assert api_list.status_code == 200
    assert [item["summary"] for item in api_list.json()["data"]] == [
        "available decision"
    ]
    assert "available decision" in html_list.text
    assert "exact-cutoff decision" not in html_list.text
    for response in (api_detail, adjacent_detail):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert html_detail.status_code == 404
    assert html_detail.headers["content-type"].startswith("text/html")
    assert available_detail.status_code == 200
