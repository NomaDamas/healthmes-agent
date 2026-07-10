"""Decision viewer rendering: decision tree -> Mermaid flowchart page (docs/PLAN.md §5).

Phase-2 implementation of the swap point left by Phase 1: the routes in
``healthmes/api/decisions.py`` keep calling ``render_*`` functions with stable
signatures, but the page is now a Jinja template (``healthmes/api/templates/``)
rendering the stored ``decision_record.tree`` — recursive
``{id, type: input|rule|llm_step|option|action, label, detail, children[]}``
nodes — as a Mermaid flowchart with a click-to-inspect detail panel, plus an
always-present escaped HTML outline (no-JS / screen-reader / render-failure
fallback). Mermaid itself is vendored (``healthmes/api/static/mermaid.min.js``)
and served by this service — no CDN, local-first.

Trust model — every string in ``tree`` is user/LLM-derived and treated as
hostile:

- HTML contexts are covered by Jinja ``autoescape=True`` on every template,
  and the JSON data island uses the ``tojson`` filter (``<``/``>``/``&``/``'``
  become ``\\uNNNN`` escapes, so ``</script>`` breakouts are impossible).
- The Mermaid source is built only from *generated* node ids (``n0``, ``n1``,
  ...) and label text passed through :func:`escape_mermaid_label`, which maps
  every character Mermaid could interpret (quote, brackets, braces, parens,
  entity ``#``, comment ``%``, pipe, backslash, backtick, ``&``/``<``/``>``,
  newlines) to Mermaid's numeric character-escape syntax (``#34;`` for ``"``
  etc.), which Mermaid decodes back to the literal character at render time.
- Node ``type`` never reaches the diagram raw: it is whitelisted to
  :data:`KNOWN_NODE_TYPES` (else ``other``) before becoming a Mermaid class
  name or a CSS class.
- The page initialises Mermaid with ``securityLevel: "strict"`` as a second
  layer, and the detail panel fills itself via ``textContent`` only.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from healthmes.api.pagination import PageMeta
from healthmes.store import DecisionRecord

__all__ = [
    "KNOWN_NODE_TYPES",
    "FALLBACK_NODE_TYPE",
    "MAX_TREE_DEPTH",
    "MAX_TREE_NODES",
    "MAX_DIAGRAM_LABEL_CHARS",
    "TreeNode",
    "DecisionTreeView",
    "escape_mermaid_label",
    "tree_to_mermaid",
    "render_decision_html",
    "render_decision_list_html",
    "render_not_found_html",
]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Guard rails against recursion bombs / browser-melting diagrams. The outline
# and the diagram both come from the same normalised tree, so the caps apply
# consistently; the full record is always available on the .json view.
MAX_TREE_DEPTH = 20
MAX_TREE_NODES = 200
# Long labels are shortened in the diagram only; the detail panel and the
# outline always show the full text.
MAX_DIAGRAM_LABEL_CHARS = 80

KNOWN_NODE_TYPES: tuple[str, ...] = ("input", "rule", "llm_step", "option", "action")
FALLBACK_NODE_TYPE = "other"
_UNTITLED_LABEL = "(untitled)"

# Mermaid shape delimiters per node type (all wrap the label in one quoted
# string, so each node line contains exactly two double quotes).
_NODE_SHAPES: dict[str, tuple[str, str]] = {
    "input": ('(["', '"])'),  # stadium
    "rule": ('{{"', '"}}'),  # hexagon
    "llm_step": ('["', '"]'),  # rectangle
    "option": ('("', '")'),  # rounded
    "action": ('[["', '"]]'),  # subroutine
    FALLBACK_NODE_TYPE: ('["', '"]'),
}

# Fills match the .type-* badge palette in templates/_base.html.j2.
_CLASS_DEFS: tuple[str, ...] = (
    "classDef type_input fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a",
    "classDef type_rule fill:#ede9fe,stroke:#6d28d9,color:#4c1d95",
    "classDef type_llm_step fill:#fef3c7,stroke:#b45309,color:#78350f",
    "classDef type_option fill:#ccfbf1,stroke:#0f766e,color:#134e4a",
    "classDef type_action fill:#dcfce7,stroke:#15803d,color:#14532d",
    "classDef type_other fill:#f4f4f5,stroke:#78716c,color:#3f3f46",
)

# Everything Mermaid could interpret inside (or as a way out of) a quoted
# label maps to Mermaid's character-escape syntax ``#<decimal>;`` (rendered
# back as the literal character). ``#`` itself is escaped, so user text can
# never smuggle its own entity; a single translate() pass means our
# replacement strings are never re-scanned. Newlines would end the statement,
# so they collapse to spaces.
_MERMAID_LABEL_TRANSLATION = str.maketrans(
    {
        "&": "#38;",
        "<": "#60;",
        ">": "#62;",
        '"': "#34;",
        "'": "#39;",
        "`": "#96;",
        "%": "#37;",
        "#": "#35;",
        ";": "#59;",
        "{": "#123;",
        "}": "#125;",
        "[": "#91;",
        "]": "#93;",
        "(": "#40;",
        ")": "#41;",
        "|": "#124;",
        "\\": "#92;",
        "\n": " ",
        "\r": " ",
        "\t": " ",
    }
)


def escape_mermaid_label(text: str) -> str:
    """Escape user-derived text for use inside a quoted Mermaid label.

    The output contains none of the characters Mermaid's flowchart grammar
    reacts to; ``#`` and ``;`` appear only as part of ``#<decimal>;`` escapes
    that Mermaid decodes back to the original character when rendering.
    """
    return text.translate(_MERMAID_LABEL_TRANSLATION)


@dataclass
class TreeNode:
    """One normalised decision-tree node (plain strings, whitelisted type)."""

    gid: str
    """Generated diagram id (``n0``, ``n1``, ... in preorder) — never user data."""
    source_id: str
    type: str
    """Normalised type: one of :data:`KNOWN_NODE_TYPES` or ``other``."""
    raw_type: str
    """The original ``type`` string, for display (escaped by the template)."""
    label: str
    detail: str
    children: list["TreeNode"] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionTreeView:
    """Everything the decision page needs, derived once from ``record.tree``."""

    root: TreeNode | None
    source: str
    """Mermaid flowchart source (empty when there is no renderable tree)."""
    node_index: dict[str, dict[str, str]]
    """gid -> full node data for the click-to-inspect panel (JSON island)."""
    truncated: bool


def _normalize_tree(tree: Any) -> tuple[TreeNode | None, bool]:
    """Walk the raw JSON tree into ``TreeNode``s (preorder gids, capped)."""
    truncated = False
    count = 0

    def visit(raw: Any, depth: int) -> TreeNode | None:
        nonlocal truncated, count
        if not isinstance(raw, dict):
            return None
        if depth > MAX_TREE_DEPTH or count >= MAX_TREE_NODES:
            truncated = True
            return None
        gid = f"n{count}"
        count += 1
        raw_type = str(raw.get("type") or "").strip()
        node_type = raw_type.lower() if raw_type.lower() in KNOWN_NODE_TYPES else FALLBACK_NODE_TYPE
        label = str(raw.get("label") or "").strip() or _UNTITLED_LABEL
        detail_value = raw.get("detail")
        detail = "" if detail_value is None else str(detail_value)
        node = TreeNode(
            gid=gid,
            source_id=str(raw.get("id") or ""),
            type=node_type,
            raw_type=raw_type,
            label=label,
            detail=detail,
        )
        children = raw.get("children")
        if isinstance(children, list):
            for child in children:
                child_node = visit(child, depth + 1)
                if child_node is not None:
                    node.children.append(child_node)
        return node

    root = visit(tree, 0)
    return root, truncated


def tree_to_mermaid(tree: Any) -> DecisionTreeView:
    """Transform a ``decision_record.tree`` JSON value into a Mermaid view.

    Pure function; safe on malformed input (non-dict nodes are skipped, depth
    and node count are capped, unknown types fall back to ``other``).
    """
    root, truncated = _normalize_tree(tree)
    if root is None:
        return DecisionTreeView(root=None, source="", node_index={}, truncated=truncated)

    lines = ["flowchart TD"]
    node_index: dict[str, dict[str, str]] = {}

    def emit(node: TreeNode, parent: TreeNode | None) -> None:
        open_mark, close_mark = _NODE_SHAPES[node.type]
        diagram_label = node.label
        if len(diagram_label) > MAX_DIAGRAM_LABEL_CHARS:
            diagram_label = diagram_label[: MAX_DIAGRAM_LABEL_CHARS - 1] + "…"
        escaped = escape_mermaid_label(diagram_label)
        lines.append(f"  {node.gid}{open_mark}{escaped}{close_mark}:::type_{node.type}")
        if parent is not None:
            lines.append(f"  {parent.gid} --> {node.gid}")
        node_index[node.gid] = {
            "source_id": node.source_id,
            "type": node.type,
            "raw_type": node.raw_type,
            "label": node.label,
            "detail": node.detail,
        }
        for child in node.children:
            emit(child, node)

    emit(root, None)
    lines.extend(f"  {class_def}" for class_def in _CLASS_DEFS)
    return DecisionTreeView(
        root=root,
        source="\n".join(lines),
        node_index=node_index,
        truncated=truncated,
    )


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Jinja environment over ``healthmes/api/templates/`` (autoescape on)."""
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _format_created(value: Any) -> str:
    """Display string for ``created_at`` (naive sqlite values are UTC by contract)."""
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def render_decision_html(record: DecisionRecord) -> str:
    """Render the full Mermaid viewer page for one decision record."""
    graph = tree_to_mermaid(record.tree)
    template = _environment().get_template("decision.html.j2")
    return template.render(
        record=record,
        graph=graph,
        created_display=_format_created(record.created_at),
        short_id=str(record.id)[:8],
        node_types=[*KNOWN_NODE_TYPES, FALLBACK_NODE_TYPE],
    )


def render_decision_list_html(
    records: Sequence[DecisionRecord],
    meta: PageMeta,
    kind: str | None = None,
) -> str:
    """Render the paginated decision index page (newest first).

    ``kind`` is the already-validated filter value (enum ``.value``) used only
    to keep the filter in pagination links.
    """
    newer_offset: int | None = max(meta.offset - meta.limit, 0) if meta.offset > 0 else None
    older_offset: int | None = meta.offset + meta.limit if meta.has_more else None

    def page_href(offset: int) -> str:
        href = f"/decisions?limit={meta.limit}&offset={offset}"
        if kind:
            href += f"&kind={kind}"
        return href

    template = _environment().get_template("decision_list.html.j2")
    return template.render(
        records=records,
        meta=meta,
        kind=kind,
        newer_offset=newer_offset,
        older_offset=older_offset,
        page_href=page_href,
        format_created=_format_created,
    )


def render_not_found_html(decision_id: str | uuid.UUID) -> str:
    """Small 404 page so alert links never dump a JSON envelope on a human."""
    template = _environment().get_template("decision_not_found.html.j2")
    return template.render(decision_id=str(decision_id))
