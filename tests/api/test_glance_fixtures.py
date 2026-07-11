"""Companion-app contract fixtures must parse against the live server models.

docs/DEVELOPMENT.md ("Companion & desktop apps", issues #7/#10/#11): every
companion pins the app-facing response schemas in fixture JSON, and a
server-side contract change must update the fixtures of **all** platforms in
the same PR. The companion suites (gradle JVM tests, XCTest, xunit) are not
part of the Python CI jobs, so this test enforces the rule from the Python
side: every in-repo fixture must validate against the server's pydantic
response model. A contract change (field rename, new enum value the fixtures
still miss, type change) fails *here* instead of leaving every suite green
while the apps break only at runtime against a live instance.

Pinned fixture sets:

- ``GET /v1/briefing/glance`` → :class:`healthmes.api.briefing.GlanceOut`
  (Android, iOS, Windows — the Windows copies are byte-identical twins).
- ``GET /v1/alerts`` → ``Page[AlertOut]`` (healthmes/api/alerts.py +
  healthmes/api/pagination.py). Each platform authored its own valid page.
- ``GET /reports/weekly.json`` → :class:`healthmes.api.reports.WeeklyReportOut`
  (Android, iOS). The Windows copy deliberately keeps the deep sections
  untyped (its desktop parser pins the envelope only), so full-model
  validation would reject it by design — the envelope fields are pinned
  separately instead.
"""

import json
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from healthmes.api.alerts import AlertOut
from healthmes.api.briefing import GlanceOut
from healthmes.api.pagination import Page
from healthmes.api.reports import REPORT_DAYS, WeeklyReportOut

REPO_ROOT = Path(__file__).resolve().parents[2]

ANDROID_FIXTURES = (
    REPO_ROOT / "apps" / "android-usage" / "companion" / "src" / "test" / "resources"
)
IOS_FIXTURES = REPO_ROOT / "apps" / "ios-companion" / "Tests" / "Fixtures"
WINDOWS_FIXTURES = (
    REPO_ROOT / "apps" / "windows-companion" / "tests" / "HealthMes.Glance.Core.Tests"
    / "Fixtures"
)

GLANCE_FIXTURES = (
    ANDROID_FIXTURES / "glance_full.json",
    ANDROID_FIXTURES / "glance_empty.json",
    IOS_FIXTURES / "glance.json",
    # Windows companion (issue #11): byte-identical copies of the three
    # fixtures above, pinned by the HealthMes.Glance.Core xunit suite.
    WINDOWS_FIXTURES / "glance_full.json",
    WINDOWS_FIXTURES / "glance_empty.json",
    WINDOWS_FIXTURES / "glance.json",
)

ALERTS_FIXTURES = (
    ANDROID_FIXTURES / "alerts_page.json",
    IOS_FIXTURES / "alerts.json",
    WINDOWS_FIXTURES / "alerts_page.json",
)

WEEKLY_REPORT_FIXTURES = (
    ANDROID_FIXTURES / "weekly_report.json",
    IOS_FIXTURES / "weekly_report.json",
)

# The Windows weekly parser is envelope-only by design (README/DEFERRED.md of
# apps/windows-companion): deep sections stay untyped JSON there, and the
# fixture reflects that with placeholder section bodies the real endpoint
# would never emit. Only the envelope is pinned for it.
WINDOWS_WEEKLY_ENVELOPE_FIXTURE = WINDOWS_FIXTURES / "weekly_report.json"

ALL_FIXTURES = (
    *GLANCE_FIXTURES,
    *ALERTS_FIXTURES,
    *WEEKLY_REPORT_FIXTURES,
    WINDOWS_WEEKLY_ENVELOPE_FIXTURE,
)


def _fixture_ids(paths: tuple[Path, ...]) -> list[str]:
    return [str(path.relative_to(REPO_ROOT)) for path in paths]


def test_every_pinned_fixture_exists() -> None:
    """Moving or renaming a fixture must fail loudly, never skip silently."""
    missing = [str(path) for path in ALL_FIXTURES if not path.is_file()]
    assert not missing, f"companion fixtures missing: {missing}"


@pytest.mark.parametrize(
    "fixture_path", GLANCE_FIXTURES, ids=_fixture_ids(GLANCE_FIXTURES)
)
def test_glance_fixture_validates_against_the_server_contract(
    fixture_path: Path,
) -> None:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    glance = GlanceOut.model_validate(payload)
    # The one structural invariant the model alone does not pin: the curve
    # always carries exactly 24 hourly entries (briefing.py contract).
    assert len(glance.energy.curve_24h) == 24


@pytest.mark.parametrize(
    "fixture_path", ALERTS_FIXTURES, ids=_fixture_ids(ALERTS_FIXTURES)
)
def test_alerts_fixture_validates_against_the_server_contract(
    fixture_path: Path,
) -> None:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    page = Page[AlertOut].model_validate(payload)
    # Envelope sanity the generic model does not enforce by itself: the page
    # is a slice of total_count (alerts.py builds PageMeta from the full set).
    assert len(page.data) <= page.pagination.total_count
    assert page.pagination.offset + len(page.data) <= page.pagination.total_count


@pytest.mark.parametrize(
    "fixture_path", WEEKLY_REPORT_FIXTURES, ids=_fixture_ids(WEEKLY_REPORT_FIXTURES)
)
def test_weekly_report_fixture_validates_against_the_server_contract(
    fixture_path: Path,
) -> None:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    report = WeeklyReportOut.model_validate(payload)
    # reports.py invariant the model alone does not pin: one energy entry per
    # day of the 7-day window, oldest first, ending on week_end.
    assert len(report.energy.days) == REPORT_DAYS
    assert report.energy.days[0].date == report.week_start
    assert report.energy.days[-1].date == report.week_end


class _WeeklyEnvelope(BaseModel):
    """Envelope half of ``WeeklyReportOut`` (what the Windows parser types).

    ``extra="forbid"`` + required section keys keep the Windows fixture from
    silently drifting away from the real response shape: a renamed or added
    top-level field in ``WeeklyReportOut`` must be mirrored here (this model
    is compared against it field-for-field below).
    """

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    timezone: str
    week_start: date
    week_end: date
    report_url: str
    energy: dict
    insights: dict
    schedule: dict
    alerts: dict
    decisions: dict


def test_windows_weekly_fixture_pins_the_envelope() -> None:
    payload = json.loads(
        WINDOWS_WEEKLY_ENVELOPE_FIXTURE.read_text(encoding="utf-8")
    )
    envelope = _WeeklyEnvelope.model_validate(payload)
    assert envelope.week_start <= envelope.week_end


def test_weekly_envelope_model_mirrors_the_real_report_model() -> None:
    """The envelope-only pin may never fall behind ``WeeklyReportOut``."""
    assert set(_WeeklyEnvelope.model_fields) == set(WeeklyReportOut.model_fields)


def test_alert_fixture_ids_are_unique_across_platforms() -> None:
    """Each platform authored its own page; a copy-paste of one platform's
    fixture into another would silently weaken the cross-platform coverage the
    apps rely on (three independently-written valid pages)."""
    seen: dict[uuid.UUID, Path] = {}
    for fixture_path in ALERTS_FIXTURES:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        for item in Page[AlertOut].model_validate(payload).data:
            assert item.id not in seen, (
                f"alert id {item.id} appears in both {seen[item.id]} and "
                f"{fixture_path}"
            )
            seen[item.id] = fixture_path
