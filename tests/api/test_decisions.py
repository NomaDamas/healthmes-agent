"""Tests for the decision viewer routes (JSON + placeholder HTML page)."""

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
