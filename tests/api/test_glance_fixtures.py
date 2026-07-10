"""Companion-app glance fixtures must parse against the live server contract.

docs/DEVELOPMENT.md ("Companion apps", issue #7): both companions pin the
``GET /v1/briefing/glance`` response schema in fixture JSON, and a server-side
change to healthmes/api/briefing.py must update
``apps/android-usage/companion/src/test/resources/glance_*.json`` and
``apps/ios-companion/Tests/Fixtures/glance.json`` in the same PR. The
companion suites (gradle JVM tests, XCTest) are not part of CI, so this test
enforces the rule from the Python side: every in-repo fixture must validate
against :class:`healthmes.api.briefing.GlanceOut`. A contract change (field
rename, new enum value the fixtures still miss, type change) fails *here*
instead of leaving every suite green while both apps break only at runtime
against a live instance.
"""

import json
from pathlib import Path

import pytest

from healthmes.api.briefing import GlanceOut

REPO_ROOT = Path(__file__).resolve().parents[2]

GLANCE_FIXTURES = (
    REPO_ROOT / "apps" / "android-usage" / "companion" / "src" / "test" / "resources"
    / "glance_full.json",
    REPO_ROOT / "apps" / "android-usage" / "companion" / "src" / "test" / "resources"
    / "glance_empty.json",
    REPO_ROOT / "apps" / "ios-companion" / "Tests" / "Fixtures" / "glance.json",
)


def test_every_pinned_fixture_exists() -> None:
    """Moving or renaming a fixture must fail loudly, never skip silently."""
    missing = [str(path) for path in GLANCE_FIXTURES if not path.is_file()]
    assert not missing, f"companion glance fixtures missing: {missing}"


@pytest.mark.parametrize(
    "fixture_path",
    GLANCE_FIXTURES,
    ids=[str(path.relative_to(REPO_ROOT)) for path in GLANCE_FIXTURES],
)
def test_fixture_validates_against_the_server_contract(fixture_path: Path) -> None:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    glance = GlanceOut.model_validate(payload)
    # The one structural invariant the model alone does not pin: the curve
    # always carries exactly 24 hourly entries (briefing.py contract).
    assert len(glance.energy.curve_24h) == 24
