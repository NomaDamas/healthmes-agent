"""Read-only access to reviewed HealthMes wellness decision skills."""

from __future__ import annotations

import hashlib
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from healthmes.hermes_mcp_inventory import (
    HERMES_DECISION_MCP_TOOL_NAMES,
)

WELLNESS_SKILL_CATALOG_VERSION = "healthmes-wellness-skills.v1"
MAX_WELLNESS_SKILL_BYTES = 64_000
REVIEWED_WELLNESS_SKILLS = (
    "healthmes-wellness-decision",
    "healthmes-caffeine",
    "healthmes-nutrition-decision",
    "healthmes-sleep",
    "healthmes-stress",
)
CANONICAL_WELLNESS_DECISION_TOOLS = HERMES_DECISION_MCP_TOOL_NAMES

_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
_MCP_TOOL_REFERENCE = re.compile(
    r"\bmcp__(?P<server>[A-Za-z0-9_-]+)__"
    r"(?P<tool>[A-Za-z0-9_-]+)\b"
)


class WellnessSkillCatalogError(ValueError):
    """A reviewed skill is unavailable or violates the catalog contract."""


def list_reviewed_wellness_skills() -> dict[str, Any]:
    """Return stable metadata without loading arbitrary repository files."""

    return {
        "schema": WELLNESS_SKILL_CATALOG_VERSION,
        "skills": [
            _skill_metadata(name)
            for name in REVIEWED_WELLNESS_SKILLS
        ],
    }


def read_reviewed_wellness_skill(name: str) -> dict[str, Any]:
    """Return one exact allowlisted skill and its content digest."""

    metadata, content = _load_skill(name)
    return {
        "schema": WELLNESS_SKILL_CATALOG_VERSION,
        "skill": metadata,
        "content": content,
    }


def _skill_metadata(name: str) -> dict[str, Any]:
    metadata, _content = _load_skill(name)
    return metadata


def _load_skill(name: str) -> tuple[dict[str, Any], str]:
    if name not in REVIEWED_WELLNESS_SKILLS:
        raise WellnessSkillCatalogError(
            "wellness_skill_not_reviewed"
        )
    encoded = _read_skill_bytes(name)
    if len(encoded) > MAX_WELLNESS_SKILL_BYTES:
        raise WellnessSkillCatalogError(
            "wellness_skill_too_large"
        )
    try:
        content = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WellnessSkillCatalogError(
            "wellness_skill_not_utf8"
        ) from exc
    _validate_tool_references(content)
    frontmatter = _frontmatter(content)
    if frontmatter.get("name") != name:
        raise WellnessSkillCatalogError(
            "wellness_skill_name_mismatch"
        )
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise WellnessSkillCatalogError(
            "wellness_skill_description_missing"
        )
    version = frontmatter.get("version")
    if version is not None and not isinstance(version, (str, int, float)):
        raise WellnessSkillCatalogError(
            "wellness_skill_version_invalid"
        )
    metadata: dict[str, Any] = {
        "name": name,
        "description": description.strip(),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }
    if version is not None:
        metadata["version"] = str(version)
    return metadata, content


def _validate_tool_references(content: str) -> None:
    allowed = frozenset(CANONICAL_WELLNESS_DECISION_TOOLS)
    for match in _MCP_TOOL_REFERENCE.finditer(content):
        if match.group("server") != "healthmes":
            raise WellnessSkillCatalogError(
                "wellness_skill_mcp_server_not_allowed"
            )
        if match.group("tool") not in allowed:
            raise WellnessSkillCatalogError(
                "wellness_skill_tool_not_allowed"
            )


def _read_skill_bytes(name: str) -> bytes:
    packaged = (
        files("healthmes")
        .joinpath("_wellness_skills")
        .joinpath(name)
        .joinpath("SKILL.md")
    )
    try:
        if packaged.is_file():
            return packaged.read_bytes()
    except OSError:
        pass

    root = _SKILLS_ROOT.resolve()
    path = (_SKILLS_ROOT / name / "SKILL.md").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WellnessSkillCatalogError(
            "wellness_skill_path_invalid"
        ) from exc
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WellnessSkillCatalogError(
            "wellness_skill_unavailable"
        ) from exc


def _frontmatter(content: str) -> dict[str, Any]:
    if not content.startswith("---\n"):
        raise WellnessSkillCatalogError(
            "wellness_skill_frontmatter_missing"
        )
    marker = content.find("\n---\n", 4)
    if marker < 0:
        raise WellnessSkillCatalogError(
            "wellness_skill_frontmatter_invalid"
        )
    try:
        parsed = yaml.safe_load(content[4:marker])
    except yaml.YAMLError as exc:
        raise WellnessSkillCatalogError(
            "wellness_skill_frontmatter_invalid"
        ) from exc
    if not isinstance(parsed, dict):
        raise WellnessSkillCatalogError(
            "wellness_skill_frontmatter_invalid"
        )
    return parsed
