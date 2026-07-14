"""Phase-2 decision viewer tests (docs/PLAN.md §5).

Covers the tree -> Mermaid transform on a 3-level fixture, hostile-string
escaping into both the Mermaid source and the HTML page, the list endpoints
(paginated, newest first), the ``.json`` suffix view, and HTTP render smoke
including the locally served Mermaid bundle.
"""

import json
import re
from datetime import datetime

from healthmes.api.decision_html import (
    FALLBACK_NODE_TYPE,
    KNOWN_NODE_TYPES,
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    escape_mermaid_label,
    tree_to_mermaid,
)
from healthmes.store import DecisionKind, DecisionRecord

# --- fixtures -------------------------------------------------------------

# Three levels: rule -> (input, llm_step) -> (option, action).
THREE_LEVEL_TREE = {
    "id": "root",
    "type": "rule",
    "label": "stress_spike rule fired",
    "detail": "stress 82 vs baseline 55",
    "children": [
        {
            "id": "in-hrv",
            "type": "input",
            "label": "night HRV below baseline",
            "detail": "rmssd 34 vs 41",
            "children": [],
        },
        {
            "id": "llm-1",
            "type": "llm_step",
            "label": "assessed afternoon load",
            "detail": "3h of meetings after 14:00",
            "children": [
                {
                    "id": "opt-1",
                    "type": "option",
                    "label": "move focus block to morning",
                    "children": [],
                },
                {
                    "id": "act-1",
                    "type": "action",
                    "label": "proposed schedule change",
                    "detail": "focus block 14:00 to 10:00",
                    "children": [],
                },
            ],
        },
    ],
}

HOSTILE_LABEL = (
    'end"] click n0 href "javascript:alert(1)" <script>alert(`x`)</script>'
    " 100% [a](b) {c} |d| \\ #quot; \nsecond%%line;'"
)

HOSTILE_TREE = {
    "id": 'r"]; click n0 "javascript:alert(1)"',
    "type": 'rule"]:::evil',
    "label": HOSTILE_LABEL,
    "detail": "<img src=x onerror=alert(1)>",
    "children": [
        {
            "id": "c1",
            "type": "input",
            "label": 'line1\nline2 `code` --> n99 %% comment "quoted"',
            "detail": 'he said "hi" & <b>bold</b>',
            "children": [],
        }
    ],
}

_ISLAND_RE = re.compile(r'<script type="application/json" id="node-data">(.*?)</script>', re.S)


def _seed_decision(
    session,
    *,
    summary: str = "Moved focus block to tomorrow",
    tree=None,
    kind: DecisionKind = DecisionKind.SCHEDULE_CHANGE,
    created_at: datetime | None = None,
    llm_model: str | None = "claude-test-1",
    tokens: int | None = 321,
) -> DecisionRecord:
    record = DecisionRecord(
        kind=kind,
        tree=THREE_LEVEL_TREE if tree is None else tree,
        summary=summary,
        llm_model=llm_model,
        tokens=tokens,
    )
    if created_at is not None:
        record.created_at = created_at
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


# --- tree -> mermaid transform ---------------------------------------------


def test_tree_to_mermaid_three_level_fixture():
    view = tree_to_mermaid(THREE_LEVEL_TREE)
    lines = view.source.splitlines()

    assert lines[0] == "flowchart TD"
    # 결정과정 only: the diagram carries rule/llm_step/option nodes; input and
    # action nodes are partitioned out into their own zones.
    assert '  n0{{"stress_spike rule fired"}}:::type_rule' in lines
    assert '  n2["assessed afternoon load"]:::type_llm_step' in lines
    assert '  n3("move focus block to morning"):::type_option' in lines
    assert not any(":::type_input" in line for line in lines if not line.startswith("  classDef"))
    assert not any(":::type_action" in line for line in lines if not line.startswith("  classDef"))
    # Edges follow the SPLICED process structure (no edges to removed nodes).
    for edge in ("  n0 --> n2", "  n2 --> n3"):
        assert edge in lines
    assert "  n0 --> n1" not in lines
    assert "  n2 --> n4" not in lines
    # A classDef exists for every palette entry.
    for node_type in (*KNOWN_NODE_TYPES, FALLBACK_NODE_TYPE):
        assert any(line.startswith(f"  classDef type_{node_type} ") for line in lines)

    # The node index still carries EVERY node for the panel and zone chips.
    assert set(view.node_index) == {"n0", "n1", "n2", "n3", "n4"}
    assert view.node_index["n0"]["source_id"] == "root"
    assert view.node_index["n0"]["detail"] == "stress 82 vs baseline 55"
    assert view.node_index["n4"]["label"] == "proposed schedule change"
    # Recorded shape vs spliced process shape, both as generated ids.
    assert view.node_index["n0"]["children"] == ["n1", "n2"]
    assert view.node_index["n0"]["process_children"] == ["n2"]
    assert view.node_index["n2"]["children"] == ["n3", "n4"]
    assert view.node_index["n2"]["process_children"] == ["n3"]
    # Zone partition: inputs panel, outcome strip, process roots.
    assert [n.gid for n in view.inputs] == ["n1"]
    assert [n.gid for n in view.actions] == ["n4"]
    assert [n.gid for n in view.process_roots] == ["n0"]
    assert view.process_root_gids == "n0"
    assert not view.truncated
    assert view.root is not None
    assert view.root.children[1].children[0].gid == "n3"


def test_partition_zones_and_ancestry_splicing():
    """The partition rules on arbitrary shapes (docs: round-5 directive).

    Inputs may nest under llm_steps, actions may sit mid-tree with process
    descendants, inputs may even carry process children — removed nodes'
    children always reattach to the removed node's position in the parent.
    """
    tree = {
        "id": "r",
        "type": "rule",
        "label": "root",
        "children": [
            {  # input with a process child -> R1 reattaches to root, slot 1
                "id": "a",
                "type": "input",
                "label": "inA",
                "children": [{"id": "r1", "type": "rule", "label": "R1", "children": []}],
            },
            {
                "id": "l",
                "type": "llm_step",
                "label": "L",
                "children": [
                    {"id": "b", "type": "input", "label": "inB", "children": []},
                    {  # option -> mid-tree action -> option: O2 reattaches to O1
                        "id": "o1",
                        "type": "option",
                        "label": "O1",
                        "children": [
                            {
                                "id": "act",
                                "type": "action",
                                "label": "ACT",
                                "children": [
                                    {"id": "o2", "type": "option", "label": "O2", "children": []}
                                ],
                            }
                        ],
                    },
                ],
            },
        ],
    }

    view = tree_to_mermaid(tree)

    # gids are preorder over the FULL tree: r=n0 a=n1 r1=n2 l=n3 b=n4 o1=n5 act=n6 o2=n7
    assert [n.gid for n in view.inputs] == ["n1", "n4"]  # document order
    assert [n.gid for n in view.actions] == ["n6"]
    assert [n.gid for n in view.process_roots] == ["n0"]
    # Splicing preserved ancestry AND order: R1 took inA's slot before L.
    assert view.node_index["n0"]["process_children"] == ["n2", "n3"]
    assert view.node_index["n3"]["process_children"] == ["n5"]
    assert view.node_index["n5"]["process_children"] == ["n7"]  # through ACT
    # The diagram contains only process nodes and spliced edges. A branching
    # node whose children carry no explicit edge_label gets auto-indexed
    # ①② (docs: round-7 edge-indexing); single children stay plain.
    lines = view.source.splitlines()
    assert "  n0 -->|①| n2" in lines and "  n0 -->|②| n3" in lines
    assert "  n3 --> n5" in lines and "  n5 --> n7" in lines
    # The effective edge label reaches the JSON island too (client reads it).
    assert view.node_index["n2"]["edge_label"] == "①"
    assert view.node_index["n3"]["edge_label"] == "②"
    assert view.node_index["n5"]["edge_label"] == ""  # single child, unlabelled
    assert not any(":::type_input" in ln for ln in lines if not ln.startswith("  classDef"))
    assert not any(":::type_action" in ln for ln in lines if not ln.startswith("  classDef"))

    # Input-only tree: no process at all, everything lands in the facts panel.
    inputs_only = tree_to_mermaid(
        {
            "id": "x",
            "type": "input",
            "label": "solo",
            "children": [{"id": "y", "type": "input", "label": "two", "children": []}],
        }
    )
    assert inputs_only.process_roots == []
    assert inputs_only.source == ""
    assert [n.gid for n in inputs_only.inputs] == ["n0", "n1"]
    assert inputs_only.root is not None  # the page still renders (zones only)


def test_tree_to_mermaid_normalises_malformed_nodes():
    tree = {
        "id": 7,  # non-string id
        "type": "Robot<>",  # unknown type
        # no label at all
        "children": [
            "just a string",  # skipped, consumes no gid
            42,  # skipped
            {"id": "k", "type": "INPUT", "label": "case-insensitive type", "children": "nope"},
        ],
    }

    view = tree_to_mermaid(tree)

    assert set(view.node_index) == {"n0", "n1"}
    assert view.node_index["n0"]["type"] == "other"
    assert view.node_index["n0"]["raw_type"] == "Robot<>"
    assert view.node_index["n0"]["label"] == "(untitled)"
    assert view.node_index["n0"]["source_id"] == "7"
    assert view.node_index["n1"]["type"] == "input"
    assert not view.truncated


def test_tree_to_mermaid_without_renderable_tree():
    for tree in (None, [], "not a tree", 5):
        view = tree_to_mermaid(tree)
        assert view.root is None
        assert view.source == ""
        assert view.node_index == {}


def test_tree_to_mermaid_depth_cap_truncates():
    tree = {"id": "leaf", "type": "action", "label": "leaf"}
    for i in range(30):
        tree = {"id": f"d{i}", "type": "rule", "label": f"level {i}", "children": [tree]}

    view = tree_to_mermaid(tree)

    assert view.truncated
    assert len(view.node_index) == MAX_TREE_DEPTH + 1


def test_tree_to_mermaid_node_cap_truncates():
    tree = {
        "id": "root",
        "type": "rule",
        "label": "wide",
        "children": [
            {"id": f"c{i}", "type": "input", "label": f"child {i}", "children": []}
            for i in range(MAX_TREE_NODES + 50)
        ],
    }

    view = tree_to_mermaid(tree)

    assert view.truncated
    assert len(view.node_index) == MAX_TREE_NODES


# --- hostile-string escaping ------------------------------------------------


def test_escape_mermaid_label_hostile_characters():
    out = escape_mermaid_label(HOSTILE_LABEL)

    for ch in "&<>\"'`%{}[]()|\\":
        assert ch not in out, f"raw {ch!r} leaked into mermaid label"
    assert "\n" not in out and "\r" not in out and "\t" not in out
    # '#' and ';' may only appear as part of a '#<decimal>;' escape.
    assert not re.search(r"#(?!\d+;)", out)
    assert out.count("#") == len(re.findall(r"#\d+;", out)) == out.count(";")


def test_escape_mermaid_label_exact_mappings():
    assert escape_mermaid_label('a "b" c') == "a #34;b#34; c"
    assert escape_mermaid_label("<b>") == "#60;b#62;"
    # User text that already looks like a mermaid entity is neutralised.
    assert escape_mermaid_label("#quot;") == "#35;quot#59;"
    assert escape_mermaid_label("one\ntwo") == "one two"


_NODE_LINE_RE = re.compile(
    r'^  n\d+(?:\(\[|\[\[|\{\{|\(|\[)"[^"]*"(?:\]\)|\]\]|\}\}|\)|\])'
    r":::type_(?:input|rule|llm_step|option|action|other)$"
)
# Edges are plain or carry an escaped label pill (예 / 아니오 / ①②): the
# label sits between pipes and can never contain a raw pipe (escaped to #124;).
_EDGE_LINE_RE = re.compile(r"^  n\d+ -->(\|[^|]+\|)? n\d+$")
_CLASSDEF_LINE_RE = re.compile(r"^  classDef type_\w+ [#\w:,-]+$")
# Chosen-option highlight: a second class assigned via generated ids only.
_CLASS_ASSIGN_RE = re.compile(r"^  class n\d+ type_chosen$")


def test_mermaid_source_grammar_survives_hostile_labels():
    view = tree_to_mermaid(HOSTILE_TREE)
    lines = view.source.splitlines()

    assert lines[0] == "flowchart TD"
    # Every statement matches the whitelist grammar we emit — hostile labels
    # cannot add statements, click handlers, comments, or extra nodes.
    for line in lines[1:]:
        assert (
            _NODE_LINE_RE.match(line)
            or _EDGE_LINE_RE.match(line)
            or _CLASSDEF_LINE_RE.match(line)
            or _CLASS_ASSIGN_RE.match(line)
        ), f"unexpected statement in mermaid source: {line!r}"
    # Quoting stays balanced: exactly one quoted label per node line.
    for line in lines:
        assert line.count('"') in (0, 2)
    assert not any(line.lstrip().startswith("click") for line in lines)
    assert "%%" not in view.source
    assert "<script>" not in view.source
    assert "alert(1)" not in view.source
    # The attempted edge injection ("--> n99") stayed inside the label text.
    assert not re.search(r"-->\s*n99", view.source)
    # Hostile type string fell back to the safe class.
    assert view.node_index["n0"]["type"] == "other"
    assert view.node_index["n0"]["raw_type"] == 'rule"]:::evil'


def test_edge_labels_explicit_and_hostile(client, session):
    """Explicit 예/아니오 edge labels flow to the island + Mermaid, and a
    hostile edge_label can neither break the grammar nor inject markup
    (docs: round-7 edge indexing)."""
    tree = {
        "type": "rule",
        "label": "root",
        "children": [
            {"type": "option", "label": "yes branch", "edge_label": "예", "detail": "채택"},
            {
                "type": "option",
                "label": "no branch",
                "edge_label": 'x"]|--> n9 <script>',  # hostile
                "detail": "기각",
            },
        ],
    }
    view = tree_to_mermaid(tree)
    # Effective labels reach the JSON island verbatim (client renders via
    # textContent — raw markup here is inert).
    assert view.node_index["n1"]["edge_label"] == "예"
    assert view.node_index["n2"]["edge_label"] == 'x"]|--> n9 <script>'
    # Mermaid emits the 예 edge and an ESCAPED hostile edge — grammar holds,
    # no raw pipe/angle-brackets/injected node escape the label.
    lines = view.source.splitlines()
    assert "  n0 -->|예| n1" in lines
    for line in lines[1:]:
        assert (
            _NODE_LINE_RE.match(line)
            or _EDGE_LINE_RE.match(line)
            or _CLASSDEF_LINE_RE.match(line)
            or _CLASS_ASSIGN_RE.match(line)
        ), f"hostile edge label broke the grammar: {line!r}"
    assert "<script>" not in view.source
    assert not re.search(r"-->\s*n9\b", view.source)  # the injected edge stayed text

    # On the rendered page the island survives and stays angle-bracket free.
    record = _seed_decision(session, tree=tree)
    html = client.get(f"/decisions/{record.id}").text
    island = _ISLAND_RE.search(html)
    assert island and "<script>" not in island.group(1)
    data = json.loads(island.group(1))
    assert data["n1"]["edge_label"] == "예"


def test_tree_canvas_has_zoom_and_toggle_controls(client, session):
    """The tree is the main stage: a zoom/pan canvas with on-canvas controls
    and a click-toggle detail popover (docs: round-7)."""
    record = _seed_decision(session)
    html = client.get(f"/decisions/{record.id}").text
    # Zoom controls + hint live inside the canvas.
    assert 'id="zoom-in"' in html and 'id="zoom-out"' in html and 'id="zoom-fit"' in html
    assert 'class="zoom-controls' in html
    # The detail popover has an explicit close affordance (× / Esc close it).
    assert 'id="node-detail-close"' in html
    # The SVG canvas is present; the JS wires a pan/zoom viewport group and
    # toggle semantics (the viewport <g> is built at runtime).
    assert 'id="forest-svg"' in html
    assert "forest-viewport" in html
    assert "function toggleDetail" in html
    assert "function fitToView" in html


def test_decision_page_escapes_hostile_strings(client, session):
    summary = 'Sum <script>window.__pwned__ = 1</script> "quoted"'
    record = _seed_decision(session, summary=summary, tree=HOSTILE_TREE)

    response = client.get(f"/decisions/{record.id}")

    assert response.status_code == 200
    html = response.text
    # Raw payloads never reach the page...
    assert "<script>window.__pwned__" not in html
    assert "<img src=x onerror=" not in html
    assert 'href="javascript:' not in html
    # ...their escaped forms do.
    assert "&lt;script&gt;window.__pwned__" in html
    # The JSON island is angle-bracket free (no </script> breakout possible)
    # while the data survives intact for the detail panel.
    island = _ISLAND_RE.search(html)
    assert island
    assert "<" not in island.group(1)
    data = json.loads(island.group(1))
    assert data["n0"]["detail"] == "<img src=x onerror=alert(1)>"
    assert data["n1"]["label"] == 'line1\nline2 `code` --> n99 %% comment "quoted"'


# --- HTTP render smoke -------------------------------------------------------


def test_decision_page_render_smoke(client, session):
    record = _seed_decision(session)

    response = client.get(f"/decisions/{record.id}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert 'id="decision-tree"' in html
    assert 'class="mermaid"' in html
    assert "flowchart TD" in html
    # Mermaid loads from this service, never a CDN.
    assert 'src="/static/mermaid.min.js"' in html
    assert "cdn" not in html.lower()
    # The outline fallback carries the tree labels (also the no-JS view).
    assert "stress_spike rule fired" in html
    assert "proposed schedule change" in html
    # The JSON island parses and matches the tree.
    island = _ISLAND_RE.search(html)
    assert island
    data = json.loads(island.group(1))
    assert len(data) == 5
    assert data["n0"]["source_id"] == "root"
    # Cross-links to the JSON view and the index.
    assert f"/decisions/{record.id}.json" in html


def test_decision_page_without_renderable_tree(client, session):
    record = _seed_decision(session, tree=[])  # runtime JSON value, not a dict

    response = client.get(f"/decisions/{record.id}")

    assert response.status_code == 200
    assert "기록된 결정 트리가 없습니다" in response.text
    assert 'class="mermaid"' not in response.text


def test_decision_page_interactive_tree_and_toggle_controls(client, session):
    """The three tree views ship with their global controls (demo UI).

    All views render from the same normalised tree: the client-built 숲 트리
    (SVG node-link forest), the Mermaid flowchart, and the semantic <details>
    리스트 rail, which keeps the tree readable without JavaScript.
    """
    record = _seed_decision(session)

    html = client.get(f"/decisions/{record.id}").text

    # Three views over one tree.
    assert 'id="forest-view"' in html
    assert 'id="graph-view"' in html
    assert 'id="list-view"' in html
    assert 'id="forest-svg"' in html
    # View switcher + expand/collapse controls.
    assert 'data-view="forest"' in html
    assert 'data-view="graph"' in html
    assert 'data-view="list"' in html
    assert ">트리</button>" in html
    assert "플로차트" in html
    assert "리스트" in html
    assert 'id="expand-all"' in html
    assert 'id="collapse-all"' in html
    assert "모두 펼치기" in html
    assert "모두 접기" in html
    # The list fallback is semantic <details>/<summary> with every node label.
    assert "<details" in html and "<summary>" in html
    for label in (
        "stress_spike rule fired",
        "night HRV below baseline",
        "assessed afternoon load",
        "move focus block to morning",
        "proposed schedule change",
    ):
        assert label in html
    # Node detail text is present for expansion (escaped by the template).
    assert "stress 82 vs baseline 55" in html
    # The forest layout consumes the tree structure from the JSON island —
    # generated ids only.
    island = _ISLAND_RE.search(html)
    assert island
    data = json.loads(island.group(1))
    assert data["n0"]["children"] == ["n1", "n2"]
    assert data["n2"]["children"] == ["n3", "n4"]
    assert data["n3"]["children"] == []
    # Ambience shell: server-picked season + daypart attributes and both
    # header switchers (🌸☀️🍂❄️ / ☀️🌆🌙, localStorage-persisted client-side).
    assert 'data-season="' in html
    assert 'data-season-picker' in html
    for season in ("spring", "summer", "autumn", "winter"):
        assert f'data-season-choice="{season}"' in html
    assert 'data-daypart="' in html
    assert 'data-daypart-picker' in html
    for daypart in ("day", "dusk", "night"):
        assert f'data-daypart-choice="{daypart}"' in html


def test_decision_page_three_zones(client, session):
    """입력 / 결정과정 / 실행 render as three distinct zones (round-5).

    입력은 입력이고, 결정과정은 결정과정이고, 실행은 실행: inputs become the
    facts panel above, only the branching judgment is visualised as a tree,
    and actions land in the outcome strip below, connected to the tree.
    """
    record = _seed_decision(session)  # THREE_LEVEL_TREE

    html = client.get(f"/decisions/{record.id}").text

    # Zone containers, in page order.
    assert 'id="zone-inputs"' in html and "고려한 입력" in html
    assert 'id="zone-process"' in html and "결정과정" in html
    assert 'id="zone-outcome"' in html
    assert (
        html.index('id="zone-inputs"')
        < html.index('id="zone-process"')
        < html.index('id="zone-outcome"')
    )
    # Inputs render as clickable metric chips (label + detail).
    assert 'class="input-chip"' in html
    assert "night HRV below baseline" in html
    assert 'data-zone-gid="n1"' in html
    # Actions render as outcome cards with their gate/status detail.
    assert 'class="outcome-card"' in html
    assert "proposed schedule change" in html
    assert "focus block 14:00 to 10:00" in html
    assert 'data-zone-gid="n4"' in html
    # A connector ties the process tree's bottom to the outcome strip.
    assert 'class="outcome-connector"' in html
    # The client tree draws the process partition only (generated root ids).
    assert 'data-process-roots="n0"' in html
    # The flowchart shows the 결정과정 only — no input/action statements.
    pre = html.split('<pre id="decision-graph"', 1)[1].split("</pre>", 1)[0]
    assert "stress_spike rule fired" in pre
    assert "night HRV below baseline" not in pre
    assert "proposed schedule change" not in pre


def test_long_labels_render_without_truncation(client, session):
    """Long 판단 questions must stay fully readable (round-6: 생략 금지).

    The island keeps the untruncated label (the client tree wraps it across
    tspan lines instead of cutting it) and the Mermaid label is broken into
    our own <br/> segments — sanitiser intact, full text preserved.
    """
    long_label = (
        "판단 1 — 오후 일정 밀도와 수면 부채를 함께 고려했을 때 "
        "지금 집중 블록을 유지하는 것이 회복에 유리한가에 대한 검토"
    )  # 60+ chars
    assert len(long_label) > 60
    record = _seed_decision(
        session,
        tree={
            "id": "r",
            "type": "rule",
            "label": long_label,
            "children": [
                {"id": "o", "type": "option", "label": "짧은 옵션", "children": []}
            ],
        },
    )

    html = client.get(f"/decisions/{record.id}").text

    # Island: the full label, no ellipsis anywhere near it.
    island = _ISLAND_RE.search(html)
    assert island
    data = json.loads(island.group(1))
    assert data["n0"]["label"] == long_label
    assert "…" not in data["n0"]["label"]
    # Mermaid: our <br/> separators (autoescaped in the pre) split the label,
    # and rejoining them restores every fragment including the tail.
    pre = html.split('<pre id="decision-graph"', 1)[1].split("</pre>", 1)[0]
    assert "&lt;br/&gt;" in pre
    rejoined = pre.replace("&lt;br/&gt;", " ")
    assert "유리한가에 대한 검토" in rejoined  # the tail survived, not cut
    assert "판단 1" in rejoined
    # No user-controlled markup was introduced by the line breaking.
    assert "<br/>" not in pre  # only the escaped form exists inside the pre


def test_decision_page_without_process_tree(client, session):
    """Input-only records: graceful 결정 분기 없음 + grouped list fallback."""
    record = _seed_decision(
        session,
        tree={
            "id": "solo",
            "type": "input",
            "label": "only input fact",
            "detail": "raw metric 42",
            "children": [],
        },
    )

    response = client.get(f"/decisions/{record.id}")

    assert response.status_code == 200
    html = response.text
    assert "결정 분기 없음" in html
    # No process tree -> no view switcher and no diagram; list stays.
    assert 'id="viewer-controls"' not in html
    assert 'class="mermaid"' not in html
    assert 'id="list-view"' in html
    # The inputs panel still shows the fact chip.
    assert 'id="zone-inputs"' in html
    assert "only input fact" in html
    assert "raw metric 42" in html


def test_interactive_tree_escapes_hostile_strings(client, session):
    """XSS hardening extends to the interactive views (list rail + 숲 SVG).

    The 숲 트리 builds its SVG client-side from the JSON island: user strings
    may only travel through textContent / attribute assignment, so the page
    must contain no HTML-parsing sink for them (pinned below by the absence
    of any innerHTML use) and the island must stay angle-bracket free.
    """
    record = _seed_decision(session, tree=HOSTILE_TREE)

    html = client.get(f"/decisions/{record.id}").text

    list_section = html.split('id="list-view"', 1)[1].split("</section>", 1)[0]
    # Raw payloads never reach any view...
    assert "<script>alert" not in html
    assert "<img src=x onerror=" not in html
    assert 'href="javascript:' not in html
    # ...the list rail renders them escaped, content intact.
    assert "&lt;script&gt;alert" in list_section  # hostile label
    assert "&lt;img src=x onerror=alert(1)&gt;" in list_section  # hostile detail
    assert "&lt;b&gt;bold&lt;/b&gt;" in list_section  # child detail
    # The hostile raw type string lands escaped in the badge, not as markup.
    assert "rule&#34;]:::evil" in list_section or "rule&quot;]:::evil" in list_section
    # The forest view's SVG labels are built via textContent only — no
    # HTML/SVG parsing sink for user strings exists anywhere on the page.
    assert "innerHTML" not in html
    assert "insertAdjacentHTML" not in html
    assert ".textContent" in html
    # The island (the forest's only data source) is angle-bracket free while
    # the hostile payload survives intact for the panel/labels.
    island = _ISLAND_RE.search(html)
    assert island
    assert "<" not in island.group(1)
    data = json.loads(island.group(1))
    assert data["n0"]["detail"] == "<img src=x onerror=alert(1)>"
    assert data["n0"]["children"] == ["n1"]
    # The hostile input child was partitioned out of the process structure.
    assert data["n0"]["process_children"] == []


def test_mermaid_asset_served_locally(client):
    response = client.get("/static/mermaid.min.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "max-age" in response.headers.get("cache-control", "")
    assert "mermaid" in response.text


# --- list endpoints (weekly-report entry point) ------------------------------


def test_decisions_list_page_paginates_newest_first(client, session):
    _seed_decision(session, summary="oldest entry", created_at=datetime(2026, 1, 1, 9, 0))
    _seed_decision(
        session,
        summary="middle entry",
        created_at=datetime(2026, 1, 2, 9, 0),
        kind=DecisionKind.ALERT,
    )
    newest = _seed_decision(session, summary="newest entry", created_at=datetime(2026, 1, 3, 9, 0))

    response = client.get("/decisions")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert html.index("newest entry") < html.index("middle entry") < html.index("oldest entry")
    assert f"/decisions/{newest.id}" in html

    page1 = client.get("/decisions", params={"limit": 2, "offset": 0}).text
    assert "newest entry" in page1 and "middle entry" in page1
    assert "oldest entry" not in page1
    assert "/decisions?limit=2&amp;offset=2" in page1  # Older link (autoescaped &)

    page2 = client.get("/decisions", params={"limit": 2, "offset": 2}).text
    assert "oldest entry" in page2
    assert "newest entry" not in page2
    assert "/decisions?limit=2&amp;offset=0" in page2  # Newer link


def test_v1_decisions_list_json_newest_first(client, session):
    _seed_decision(session, summary="first", created_at=datetime(2026, 1, 1, 9, 0))
    _seed_decision(
        session,
        summary="second",
        created_at=datetime(2026, 1, 2, 9, 0),
        kind=DecisionKind.ALERT,
    )
    _seed_decision(session, summary="third", created_at=datetime(2026, 1, 3, 9, 0))

    response = client.get("/v1/decisions")

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["total_count"] == 3
    assert [item["summary"] for item in body["data"]] == ["third", "second", "first"]
    # List items omit the tree payload.
    assert "tree" not in body["data"][0]

    paged = client.get("/v1/decisions", params={"limit": 1, "offset": 1}).json()
    assert [item["summary"] for item in paged["data"]] == ["second"]
    assert paged["pagination"]["has_more"] is True

    filtered = client.get("/v1/decisions", params={"kind": "alert"}).json()
    assert [item["summary"] for item in filtered["data"]] == ["second"]
    assert filtered["pagination"]["total_count"] == 1


def test_decision_json_suffix_matches_v1(client, session):
    record = _seed_decision(session)

    suffix = client.get(f"/decisions/{record.id}.json")
    v1 = client.get(f"/v1/decisions/{record.id}")

    assert suffix.status_code == 200
    assert v1.status_code == 200
    assert suffix.json() == v1.json()
    assert suffix.json()["tree"] == THREE_LEVEL_TREE


def test_decision_json_suffix_404_envelope(client):
    response = client.get("/decisions/00000000-0000-0000-0000-000000000000.json")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
