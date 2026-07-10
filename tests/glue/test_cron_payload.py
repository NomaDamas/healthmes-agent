"""Cron briefing payloads vs the real vendor contract.

Asserts, against vendor/hermes-agent/cron/jobs.py imported from disk:
1. every kwarg bootstrap passes to ``create_job`` exists in its signature
   (via ``inspect.signature``),
2. the payload-fallback job dict has exactly the same key set as a job
   produced by the real ``create_job`` (called with an interval schedule,
   which needs no croniter),
3. jobs written by the fallback writer are readable by the vendor's own
   ``load_jobs`` in the same envelope.
"""

import inspect
from datetime import datetime

import pytest


@pytest.fixture(scope="session")
def briefing_jobs(bootstrap):
    return bootstrap.BRIEFING_JOBS


def test_briefing_kwargs_match_create_job_signature(vendor_cron, briefing_jobs):
    vendor_jobs, _home = vendor_cron
    signature = inspect.signature(vendor_jobs.create_job)
    for job in briefing_jobs:
        unknown = set(job) - set(signature.parameters)
        assert not unknown, f"kwargs not accepted by create_job: {unknown}"
        # prompt + schedule are the only required (non-defaulted) parameters;
        # every briefing carries both.
        assert job["prompt"] and job["schedule"]


def test_briefing_schedules_are_cron_expressions(vendor_cron, briefing_jobs):
    """The registered schedule strings parse as cron-kind schedules.

    ``parse_schedule`` recognizes the 5-field shape before needing croniter;
    without croniter it raises the explicit install hint, which still proves
    the expression took the cron branch (not a typo'd duration/timestamp).
    """
    vendor_jobs, _home = vendor_cron
    for job in briefing_jobs:
        if vendor_jobs.HAS_CRONITER:
            parsed = vendor_jobs.parse_schedule(job["schedule"])
            assert parsed["kind"] == "cron"
            assert parsed["expr"] == job["schedule"]
        else:
            with pytest.raises(ValueError, match="croniter"):
                vendor_jobs.parse_schedule(job["schedule"])


def test_briefing_scripts_point_at_the_repo_snapshot(bootstrap, briefing_jobs):
    """Every briefing pre-injects the state snapshot (PLAN §4 `script:`);
    the referenced file exists in the repo (bootstrap installs a copy into
    $HERMES_HOME/scripts/, the only directory the scheduler runs from)."""
    assert bootstrap.SNAPSHOT_SCRIPT_SOURCE.is_file()
    for job in briefing_jobs:
        assert job["script"] == bootstrap.SNAPSHOT_SCRIPT_NAME


def test_fallback_payload_matches_real_create_job_keys(vendor_cron, bootstrap):
    vendor_jobs, _home = vendor_cron
    real_job = vendor_jobs.create_job(
        prompt="signature probe",
        schedule="every 60m",  # interval schedule: valid without croniter
        name="glue-signature-probe",
        deliver="telegram",
        skills=["healthmes-planner"],
        script=bootstrap.SNAPSHOT_SCRIPT_NAME,
    )
    fallback_job = bootstrap.build_fallback_job(
        prompt="fallback probe",
        schedule="0 7 * * *",
        name="glue-fallback-probe",
        deliver="telegram",
        skills=["healthmes-planner"],
        script=bootstrap.SNAPSHOT_SCRIPT_NAME,
    )
    assert set(fallback_job) == set(real_job)

    # Value-shape parity on the scheduler-critical fields.
    assert real_job["skill"] == fallback_job["skill"] == "healthmes-planner"
    assert real_job["skills"] == fallback_job["skills"] == ["healthmes-planner"]
    assert real_job["deliver"] == fallback_job["deliver"] == "telegram"
    assert (
        real_job["script"]
        == fallback_job["script"]
        == "healthmes_briefing_snapshot.py"
    )
    assert real_job["repeat"] == fallback_job["repeat"] == {
        "times": None,
        "completed": 0,
    }
    assert real_job["state"] == fallback_job["state"] == "scheduled"
    assert real_job["enabled"] is fallback_job["enabled"] is True
    for job in (real_job, fallback_job):
        datetime.fromisoformat(job["created_at"])
        datetime.fromisoformat(job["next_run_at"])
        assert isinstance(job["schedule"], dict) and "kind" in job["schedule"]


def test_fallback_writer_output_is_loadable_by_vendor(vendor_cron, bootstrap):
    vendor_jobs, home = vendor_cron
    plan = bootstrap.Plan(dry_run=False)
    method = bootstrap.register_cron_jobs(home, plan)
    assert method in {"vendor-create_job", "payload-fallback", "no-op"}

    loaded = {
        job["name"]: job
        for job in vendor_jobs.load_jobs()
        if str(job.get("name", "")).startswith("healthmes-")
    }
    assert set(loaded) == {
        "healthmes-morning-plan",
        "healthmes-evening-review",
        "healthmes-weekly-plan",
    }
    for job in loaded.values():
        assert job["schedule"]["kind"] == "cron"
        assert job["skills"] == ["healthmes-planner"]
        assert job["deliver"] == "telegram"
        assert job["script"] == "healthmes_briefing_snapshot.py"

    # Re-registration is a no-op (matched by name).
    plan2 = bootstrap.Plan(dry_run=False)
    assert bootstrap.register_cron_jobs(home, plan2) == "no-op"
    still = [
        job
        for job in vendor_jobs.load_jobs()
        if str(job.get("name", "")).startswith("healthmes-")
    ]
    assert len(still) == 3
