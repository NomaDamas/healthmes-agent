"""Decision viewer rendering: decision tree -> viewer pages (docs/PLAN.md §5).

Phase-2 implementation of the swap point left by Phase 1: the routes in
``healthmes/api/decisions.py`` keep calling ``render_*`` functions with stable
signatures, but the pages are Jinja templates (``healthmes/api/templates/ui/``)
rendering the stored ``decision_record.tree`` — recursive
``{id, type: input|rule|llm_step|option|action, label, detail, children[]}``
nodes — as TWO views built from the same normalised tree:

- an interactive, collapsible tree ("인터랙티브 트리") of semantic
  ``<details>``/``<summary>`` nodes — also the no-JS / screen-reader /
  render-failure representation;
- the Mermaid flowchart with a click-to-inspect detail panel. Mermaid itself
  is vendored (``healthmes/api/static/mermaid.min.js``) and served by this
  service — no CDN, local-first.

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

Presentation-only inference: an ``option`` node is highlighted as *chosen*
when the tree continues through it (it has children) or its detail text
starts with an adoption marker (``채택``/``선택``/``chosen``/...); rejection
markers (``기각``/``rejected``/...) dim it. Pure display heuristics — the
stored record is never modified.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from healthmes.api.auth import viewer_token
from healthmes.api.common import ensure_utc, utc_now
from healthmes.api.pagination import PageMeta
from healthmes.config import Settings, resolve_timezone
from healthmes.store import DecisionRecord

__all__ = [
    "KNOWN_NODE_TYPES",
    "FALLBACK_NODE_TYPE",
    "NODE_TYPE_LABELS",
    "KIND_LABELS",
    "SEASONS",
    "DAYPARTS",
    "season_for_month",
    "daypart_for_hour",
    "MAX_TREE_DEPTH",
    "MAX_TREE_NODES",
    "MAX_DIAGRAM_LABEL_CHARS",
    "TreeNode",
    "DecisionTreeView",
    "partition_tree",
    "escape_mermaid_label",
    "format_created",
    "format_created_local",
    "format_relative",
    "template_environment",
    "tree_to_mermaid",
    "viewer_query_suffix",
    "shell_context",
    "render_decision_html",
    "render_decision_list_html",
    "render_not_found_html",
    "render_index_html",
]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Guard rails against recursion bombs / browser-melting diagrams. The
# interactive tree and the diagram both come from the same normalised tree, so
# the caps apply consistently; the full record is always on the .json view.
MAX_TREE_DEPTH = 20
MAX_TREE_NODES = 200
# Long labels are shortened in the diagram only; the detail panel and the
# interactive tree always show the full text.
MAX_DIAGRAM_LABEL_CHARS = 80

KNOWN_NODE_TYPES: tuple[str, ...] = ("input", "rule", "llm_step", "option", "action")
FALLBACK_NODE_TYPE = "other"
_UNTITLED_LABEL = "(untitled)"

# Korean display labels for the whitelisted node types (the raw type string is
# still shown for unknown types, escaped by the template).
NODE_TYPE_LABELS: dict[str, str] = {
    "input": "입력",
    "rule": "규칙",
    "llm_step": "판단",
    "option": "옵션",
    "action": "실행",
    FALLBACK_NODE_TYPE: "기타",
}

# Korean display labels for DecisionKind values (enum values stay English in
# every JSON payload; this map is display-only). Framing: the viewer surface
# presents decisions as proposals/feedback ("제안 · 피드백 기록") — the tree
# is the reference explanation ("궁금하면 보는 근거") behind each one.
KIND_LABELS: dict[str, str] = {
    "schedule_change": "일정 조정",
    "alert": "선제 알림",
    "insight": "하루 피드백",
    "capture": "기록",
}

# Seasonal scenery (bright daylight backdrops, docs: round-3 demo directive).
# Auto-selected from the current month in the user's timezone; the client-side
# header switcher (localStorage) may override — display-only state.
SEASONS: tuple[str, ...] = ("spring", "summer", "autumn", "winter")

# Time-of-day ambience composed over the season (12 combos via CSS custom
# properties: season = hue family, daypart = sky/light/glass/ink).
DAYPARTS: tuple[str, ...] = ("day", "dusk", "night")


def daypart_for_hour(hour: int) -> str:
    """Map a 0-23 local hour to the ambience (06-17 낮 / 17-20 초저녁 / else 밤)."""
    if 6 <= hour < 17:
        return "day"
    if 17 <= hour < 20:
        return "dusk"
    return "night"


def season_for_month(month: int) -> str:
    """Map a 1-12 month to the backdrop season (3-5 봄 / 6-8 여름 / 9-11 가을)."""
    if 3 <= month <= 5:
        return "spring"
    if 6 <= month <= 8:
        return "summer"
    if 9 <= month <= 11:
        return "autumn"
    return "winter"

# Display-only markers deciding whether an option node reads as chosen or
# rejected (checked against the start of the node's stripped detail text).
_CHOSEN_PREFIXES = ("채택", "선택", "확정", "chosen", "selected", "accepted", "adopted")
_REJECTED_PREFIXES = ("기각", "반려", "rejected", "declined", "discarded", "dismissed")

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

# Fills match the .type-* badge palette in templates/ui/_base.html.j2.
# type_chosen is additive: emitted as a second class on chosen option nodes.
_CLASS_DEFS: tuple[str, ...] = (
    "classDef type_input fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a",
    "classDef type_rule fill:#ede9fe,stroke:#6d28d9,color:#4c1d95",
    "classDef type_llm_step fill:#fef3c7,stroke:#b45309,color:#78350f",
    "classDef type_option fill:#ccfbf1,stroke:#0f766e,color:#134e4a",
    "classDef type_action fill:#dcfce7,stroke:#15803d,color:#14532d",
    "classDef type_other fill:#f4f4f5,stroke:#78716c,color:#3f3f46",
    "classDef type_chosen stroke:#0d7d69,stroke-width:3px",
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


def _char_units(ch: str) -> float:
    """Rough display width of one char (Korean/CJK = full-width = 1.0)."""
    if ch.isspace():
        return 0.34
    return 1.0 if ord(ch) > 0x2E7F else 0.56


def _wrap_units(text: str, max_units: float, max_lines: int) -> list[str]:
    """Greedy width-estimated word wrap (long words hard-break by char).

    Used for the Mermaid diagram labels (joined with ``<br/>`` AFTER each
    line went through :func:`escape_mermaid_label`) so long 판단 questions
    stay fully readable instead of being cut. Beyond ``max_lines`` the last
    line is clamped with an ellipsis — the full text always remains in the
    node detail panel and the list rail.
    """
    lines: list[str] = []
    current = ""
    current_units = 0.0

    def units(chunk: str) -> float:
        return sum(_char_units(ch) for ch in chunk)

    def commit() -> None:
        nonlocal current, current_units
        if current:
            lines.append(current)
        current = ""
        current_units = 0.0

    for word in text.split():
        word_units = units(word)
        if current and current_units + 0.34 + word_units > max_units:
            commit()
        if word_units > max_units:
            for ch in word:
                w = _char_units(ch)
                if current and current_units + w > max_units:
                    commit()
                current += ch
                current_units += w
            continue
        current = f"{current} {word}" if current else word
        current_units += (0.34 if current_units else 0.0) + word_units
    commit()
    if not lines:
        return [text.strip() or text]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


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
    chosen: bool = False
    """Display heuristic: option the decision continued through / adopted."""
    rejected: bool = False
    """Display heuristic: option whose detail starts with a rejection marker."""
    children: list["TreeNode"] = field(default_factory=list)
    """Original tree structure (all zones), exactly as recorded."""
    process_children: list["TreeNode"] = field(default_factory=list)
    """Partitioned 결정과정 structure: rule/llm_step/option/other children
    with removed input/action nodes spliced out (their children reattach
    here, order preserved)."""


@dataclass(frozen=True)
class DecisionTreeView:
    """Everything the decision page needs, derived once from ``record.tree``.

    The tree is partitioned into three zones (입력은 입력, 결정과정은
    결정과정, 실행은 실행): ``inputs`` and ``actions`` are flat preorder
    lists rendered as the facts panel above and the outcome strip below;
    ``process_roots`` is the branching judgment — the only part visualised
    as a tree (Mermaid source and the client tree both use it).
    """

    root: TreeNode | None
    source: str
    """Process-only Mermaid source (empty when there is no process tree)."""
    node_index: dict[str, dict[str, Any]]
    """gid -> full node data for the click-to-inspect panel (JSON island)."""
    truncated: bool
    inputs: list[TreeNode] = field(default_factory=list)
    """Every type=input node anywhere in the tree, document order."""
    actions: list[TreeNode] = field(default_factory=list)
    """Every type=action node anywhere in the tree, document order."""
    process_roots: list[TreeNode] = field(default_factory=list)
    """Roots of the spliced 결정과정 tree (may be several or none)."""

    @property
    def process_root_gids(self) -> str:
        """Space-joined generated root ids for the client layout (safe)."""
        return " ".join(node.gid for node in self.process_roots)


def _detail_starts_with(detail: str, prefixes: tuple[str, ...]) -> bool:
    stripped = detail.strip().lower()
    return stripped.startswith(prefixes)


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
        if node.type == "option":
            # Chosen-option inference (display-only): the tree continued below
            # this option, or its detail opens with an adoption marker.
            node.chosen = bool(node.children) or _detail_starts_with(detail, _CHOSEN_PREFIXES)
            node.rejected = not node.chosen and _detail_starts_with(detail, _REJECTED_PREFIXES)
        return node

    root = visit(tree, 0)
    return root, truncated


def partition_tree(
    root: TreeNode | None,
) -> tuple[list[TreeNode], list[TreeNode], list[TreeNode]]:
    """Partition a normalised tree into the three page zones.

    입력은 입력, 결정과정은 결정과정, 실행은 실행:

    - every ``type=input`` node (anywhere) goes to the facts panel;
    - every ``type=action`` node (anywhere) goes to the outcome strip;
    - what remains (rule / llm_step / option / unknown) is the 결정과정 tree.
      Removing an input/action node splices its children into the removed
      node's position in the parent (order preserved, recursively), so
      ancestry survives arbitrary shapes: inputs nested under llm_steps,
      actions mid-tree with process descendants, even input-only trees
      (which yield no process roots at all).

    Returns ``(inputs, actions, process_roots)``; inputs/actions are in
    document (preorder) order and every process node gets its
    ``process_children`` filled in. Pure and idempotent per normalise run.
    """
    inputs: list[TreeNode] = []
    actions: list[TreeNode] = []

    def splice(node: TreeNode) -> list[TreeNode]:
        if node.type == "input":
            inputs.append(node)
        elif node.type == "action":
            actions.append(node)
        gathered: list[TreeNode] = []
        for child in node.children:
            gathered.extend(splice(child))
        if node.type in ("input", "action"):
            return gathered  # children take this node's place upstream
        node.process_children = gathered
        return [node]

    process_roots = splice(root) if root is not None else []
    return inputs, actions, process_roots


def tree_to_mermaid(tree: Any) -> DecisionTreeView:
    """Transform a ``decision_record.tree`` JSON value into the zoned view.

    Pure function; safe on malformed input (non-dict nodes are skipped, depth
    and node count are capped, unknown types fall back to ``other``). The
    JSON island (``node_index``) keeps EVERY node — the inputs panel, the
    outcome strip and the detail panel all read from it — while the Mermaid
    source is built from the 결정과정 partition only.
    """
    root, truncated = _normalize_tree(tree)
    if root is None:
        return DecisionTreeView(root=None, source="", node_index={}, truncated=truncated)

    inputs, actions, process_roots = partition_tree(root)
    node_index: dict[str, dict[str, Any]] = {}

    def index(node: TreeNode) -> None:
        node_index[node.gid] = {
            "source_id": node.source_id,
            "type": node.type,
            "raw_type": node.raw_type,
            "label": node.label,
            "detail": node.detail,
            "chosen": node.chosen,
            "rejected": node.rejected,
            # Structure travels as generated ids only, so the island carries
            # no user-controlled structure: "children" is the recorded shape,
            # "process_children" the spliced 결정과정 the client tree draws.
            "children": [child.gid for child in node.children],
            "process_children": [child.gid for child in node.process_children],
        }
        for child in node.children:
            index(child)

    index(root)

    source = ""
    if process_roots:
        lines = ["flowchart TD"]
        chosen_lines: list[str] = []

        def emit(node: TreeNode, parent: TreeNode | None) -> None:
            open_mark, close_mark = _NODE_SHAPES[node.type]
            diagram_label = node.label
            if len(diagram_label) > MAX_DIAGRAM_LABEL_CHARS:
                diagram_label = diagram_label[: MAX_DIAGRAM_LABEL_CHARS - 1] + "…"
            # Full-width-aware wrapping; each line is sanitised separately and
            # only OUR <br/> separators exist in the label (user <> is inert).
            escaped = "<br/>".join(
                escape_mermaid_label(line)
                for line in _wrap_units(diagram_label, max_units=15.0, max_lines=4)
            )
            lines.append(f"  {node.gid}{open_mark}{escaped}{close_mark}:::type_{node.type}")
            if parent is not None:
                lines.append(f"  {parent.gid} --> {node.gid}")
            if node.chosen:
                # Generated ids only — never user data (whitelisted grammar).
                chosen_lines.append(f"  class {node.gid} type_chosen")
            for child in node.process_children:
                emit(child, node)

        for process_root in process_roots:
            emit(process_root, None)
        lines.extend(chosen_lines)
        lines.extend(f"  {class_def}" for class_def in _CLASS_DEFS)
        source = "\n".join(lines)

    return DecisionTreeView(
        root=root,
        source=source,
        node_index=node_index,
        truncated=truncated,
        inputs=inputs,
        actions=actions,
        process_roots=process_roots,
    )


@lru_cache(maxsize=1)
def template_environment() -> Environment:
    """Jinja environment over ``healthmes/api/templates/`` (autoescape on).

    Public on purpose: the weekly report (healthmes/api/reports.py) renders
    from the same templates/ directory — sibling modules import this, never a
    private name (one copy, public names).
    """
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def format_created(value: Any) -> str:
    """Display string for ``created_at`` (naive sqlite values are UTC by contract).

    Shared with the weekly report template — public for the same reason as
    :func:`template_environment`.
    """
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def format_created_local(value: Any, tz: tzinfo | None) -> str:
    """``created_at`` in the user's timezone (``Settings.timezone``).

    Naive sqlite values are UTC by contract; ``tz=None`` keeps the UTC
    rendering so render helpers stay callable without Settings (tests).
    """
    if value is None:
        return "—"
    if tz is None:
        return format_created(value)
    return ensure_utc(value).astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def format_relative(value: Any, now: datetime | None = None) -> str:
    """Compact Korean relative-time string ("방금 전", "3시간 전", ...).

    Display-only; anything older than four weeks falls back to the date.
    """
    if value is None:
        return "—"
    reference = ensure_utc(now) if now is not None else utc_now()
    moment = ensure_utc(value)
    seconds = (reference - moment).total_seconds()
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return "방금 전"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}분 전"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours}시간 전"
    days = int(seconds // 86400)
    if days < 28:
        return f"{days}일 전"
    return moment.strftime("%Y-%m-%d")


def viewer_query_suffix(settings: Settings | None) -> str:
    """``?token=<derived viewer token>`` for same-origin viewer navigation.

    The relative-href twin of :func:`healthmes.api.auth.viewer_url` (which
    stays the single construction point for *absolute* links the system emits
    into alerts). In-page navigation between viewer pages must keep the
    derived read-only credential — never the full API token — attached, or a
    tap on "전체 결정 기록" from a phone browser would 401. Empty when no API
    token is configured (open loopback dev) or no settings are supplied.
    """
    if settings is None:
        return ""
    api_token = settings.api_token.get_secret_value().strip()
    if not api_token:
        return ""
    return f"?token={viewer_token(api_token)}"


def shell_context(settings: Settings | None) -> dict[str, Any]:
    """Context shared by every viewer page: nav token suffix + user-tz helpers.

    Public for the same reason as :func:`template_environment` — the weekly
    report (healthmes/api/reports.py) renders from the same shell.
    """
    tz = resolve_timezone(settings) if settings is not None else UTC
    now = utc_now()
    return {
        "token_qs": viewer_query_suffix(settings),
        "tz_name": str(tz),
        "format_local": lambda value: format_created_local(value, tz),
        "format_rel": lambda value: format_relative(value, now),
        "kind_labels": KIND_LABELS,
        "node_type_labels": NODE_TYPE_LABELS,
        # Seasonal backdrop + time-of-day ambience, auto-picked from the
        # user's local clock; the client-side switchers may override
        # (localStorage, display-only).
        "season": season_for_month(now.astimezone(tz).month),
        "daypart": daypart_for_hour(now.astimezone(tz).hour),
    }


def render_decision_html(record: DecisionRecord, settings: Settings | None = None) -> str:
    """Render the decision page (interactive tree + Mermaid view) for one record."""
    graph = tree_to_mermaid(record.tree)
    template = template_environment().get_template("ui/decision.html.j2")
    return template.render(
        record=record,
        graph=graph,
        short_id=str(record.id)[:8],
        node_types=[*KNOWN_NODE_TYPES, FALLBACK_NODE_TYPE],
        **shell_context(settings),
    )


def render_decision_list_html(
    records: Sequence[DecisionRecord],
    meta: PageMeta,
    kind: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Render the paginated decision index page (newest first).

    ``kind`` is the already-validated filter value (enum ``.value``) used only
    to keep the filter in pagination links.
    """
    newer_offset: int | None = max(meta.offset - meta.limit, 0) if meta.offset > 0 else None
    older_offset: int | None = meta.offset + meta.limit if meta.has_more else None
    shell = shell_context(settings)
    token_pair = shell["token_qs"].replace("?", "&", 1)

    def page_href(offset: int) -> str:
        href = f"/decisions?limit={meta.limit}&offset={offset}"
        if kind:
            href += f"&kind={kind}"
        return href + token_pair

    def kind_href(value: str | None) -> str:
        href = "/decisions" if not value else f"/decisions?kind={value}"
        if not value:
            return href + shell["token_qs"]
        return href + token_pair

    template = template_environment().get_template("ui/decision_list.html.j2")
    return template.render(
        records=records,
        meta=meta,
        kind=kind,
        newer_offset=newer_offset,
        older_offset=older_offset,
        page_href=page_href,
        kind_href=kind_href,
        kind_values=list(KIND_LABELS),
        **shell,
    )


def render_not_found_html(
    decision_id: str | uuid.UUID, settings: Settings | None = None
) -> str:
    """Small 404 page so alert links never dump a JSON envelope on a human."""
    template = template_environment().get_template("ui/decision_not_found.html.j2")
    return template.render(decision_id=str(decision_id), **shell_context(settings))


def render_index_html(settings: Settings | None = None) -> str:
    """Static landing shell for ``GET /`` — links only, no data, no credentials.

    Deliberately renders NO viewer token and reads NO store data: the shell is
    safe to serve to anyone the auth middleware lets through, today and under
    any future auth posture for ``/`` (docs/PLAN.md §9 stays untouched here).
    ``settings`` is used ONLY to pick the seasonal backdrop from the user's
    timezone — never for credentials.
    """
    tz = resolve_timezone(settings) if settings is not None else UTC
    local_now = utc_now().astimezone(tz)
    template = template_environment().get_template("ui/index.html.j2")
    return template.render(
        token_qs="",
        season=season_for_month(local_now.month),
        daypart=daypart_for_hour(local_now.hour),
    )
