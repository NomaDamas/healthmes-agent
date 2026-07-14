"""Tests for scripts/bootstrap.py.

Covers: template rendering into a tmp HERMES_HOME, skill copy-install into
the Hermes discovery path, secret generation into .env, cron briefing
registration, idempotency of the whole pipeline, --dry-run inertness, and
docker-mode defaults. Everything runs against tmp paths — the developer's
real ~/.hermes is never touched.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.usefixtures("clean_env")


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456:test-token\n"
        "OPEN_WEARABLES_API_KEY=ow-test-key\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    return tmp_path / "hermes-home"


def run_bootstrap(bootstrap, hermes_home: Path, env_file: Path, *extra: str) -> int:
    return bootstrap.main(
        ["--hermes-home", str(hermes_home), "--env-file", str(env_file), *extra]
    )


# ---------------------------------------------------------------------------
# Full native run
# ---------------------------------------------------------------------------


def test_full_run_builds_expected_tree(bootstrap, hermes_home, env_file, capsys):
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    # 1. config.yaml rendered and parseable, with vendor-contract keys.
    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert config["platforms"]["telegram"]["token"] == "123456:test-token"
    route = config["platforms"]["webhook"]["extra"]["routes"]["healthmes-alerts"]
    assert route["skills"] == ["healthmes-planner"]
    assert route["deliver"] == "telegram"

    # The generated HMAC secret is shared between .env and the route.
    env_values = bootstrap.load_env_file(env_file)
    secret = env_values["HEALTHMES_HERMES_WEBHOOK_SECRET"]
    assert secret and route["secret"] == secret

    # Native defaults: localhost endpoints, repo-local vendored MCP dir.
    servers = config["mcp_servers"]
    assert servers["healthmes"]["url"] == "http://localhost:8100/mcp"
    ow = servers["open_wearables"]
    assert ow["env"]["OPEN_WEARABLES_API_URL"] == "http://localhost:8000"
    assert ow["env"]["OPEN_WEARABLES_API_KEY"] == "ow-test-key"
    assert str(REPO_ROOT / "vendor" / "open-wearables" / "mcp") in ow["args"]

    # 2. Skills copied into the discovery path (SKILLS_DIR = home/skills).
    # Copies, not symlinks: the vendor trust check resolves symlinks and
    # would log a security warning on every skill load (skills_tool.py).
    for skill in (
        "healthmes-planner",
        "healthmes-capture",
        "healthmes-sleep",
        "doctor-visit-summary",
    ):
        dest = hermes_home / "skills" / skill
        assert dest.is_dir() and not dest.is_symlink()
        assert (dest / "SKILL.md").read_text() == (
            REPO_ROOT / "skills" / skill / "SKILL.md"
        ).read_text()

    # 3. Briefing snapshot script + base-url sidecar installed into the
    # scheduler's only allowed script location ($HERMES_HOME/scripts/).
    installed_script = hermes_home / "scripts" / "healthmes_briefing_snapshot.py"
    assert installed_script.is_file()
    assert installed_script.read_text() == (
        REPO_ROOT / "scripts" / "healthmes_briefing_snapshot.py"
    ).read_text()
    sidecar = yaml.safe_load((hermes_home / "scripts" / "healthmes_snapshot.json").read_text())
    assert sidecar == {"base_url": "http://localhost:8100"}  # native default

    # No api token configured: the MCP registration carries no auth header
    # and the sidecar carries no token key.
    assert "headers" not in servers["healthmes"]

    # 4. Cron briefings registered in the vendor jobs.json envelope.
    jobs_doc = yaml.safe_load((hermes_home / "cron" / "jobs.json").read_text())
    assert set(jobs_doc) >= {"jobs", "updated_at"}
    jobs = {job["name"]: job for job in jobs_doc["jobs"]}
    assert set(jobs) == {
        "healthmes-morning-plan",
        "healthmes-evening-review",
        "healthmes-weekly-plan",
    }
    assert jobs["healthmes-morning-plan"]["schedule"]["expr"] == "0 7 * * *"
    assert jobs["healthmes-evening-review"]["schedule"]["expr"] == "30 21 * * *"
    assert jobs["healthmes-weekly-plan"]["schedule"]["expr"] == "0 18 * * 0"
    for job in jobs.values():
        assert job["skills"] == ["healthmes-planner"]
        assert job["deliver"] == "telegram"
        assert job["enabled"] is True
        # Context-injection script (PLAN §4): relative name resolving under
        # $HERMES_HOME/scripts/ — exactly where step 3 installed it.
        assert job["script"] == "healthmes_briefing_snapshot.py"
        # next_run_at is a parseable timestamp (scheduler contract).
        datetime.fromisoformat(job["next_run_at"])

    out = capsys.readouterr().out
    assert "cron registration method:" in out


def test_second_run_is_idempotent(bootstrap, hermes_home, env_file):
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    config_before = (hermes_home / "config.yaml").read_text()
    jobs_before = yaml.safe_load((hermes_home / "cron" / "jobs.json").read_text())
    env_before = env_file.read_text()

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    assert (hermes_home / "config.yaml").read_text() == config_before
    jobs_after = yaml.safe_load((hermes_home / "cron" / "jobs.json").read_text())
    assert len(jobs_after["jobs"]) == 3
    assert [j["id"] for j in jobs_after["jobs"]] == [
        j["id"] for j in jobs_before["jobs"]
    ]
    assert env_file.read_text() == env_before


def test_dry_run_writes_nothing(bootstrap, hermes_home, env_file, capsys):
    env_before = env_file.read_text()
    assert run_bootstrap(bootstrap, hermes_home, env_file, "--dry-run") == 0

    assert not hermes_home.exists()
    assert env_file.read_text() == env_before

    out = capsys.readouterr().out
    # The dry-run still reports the full expected tree.
    assert "config.yaml" in out
    assert "healthmes-planner" in out
    assert "healthmes-morning-plan" in out
    assert out.count("[dry-run] would") >= 5


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_existing_secret_is_preserved(bootstrap, hermes_home, env_file):
    env_file.write_text(
        env_file.read_text() + "HEALTHMES_HERMES_WEBHOOK_SECRET=keep-me\n",
        encoding="utf-8",
    )
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    assert bootstrap.load_env_file(env_file)["HEALTHMES_HERMES_WEBHOOK_SECRET"] == "keep-me"
    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    route = config["platforms"]["webhook"]["extra"]["routes"]["healthmes-alerts"]
    assert route["secret"] == "keep-me"


def test_empty_secret_assignment_is_filled_in_place(bootstrap, hermes_home, env_file):
    env_file.write_text(
        env_file.read_text() + "HEALTHMES_HERMES_WEBHOOK_SECRET=\n",
        encoding="utf-8",
    )
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    content = env_file.read_text()
    assert content.count("HEALTHMES_HERMES_WEBHOOK_SECRET=") == 1
    assert bootstrap.load_env_file(env_file)["HEALTHMES_HERMES_WEBHOOK_SECRET"]


def test_missing_env_file_is_created_for_secret(bootstrap, hermes_home, tmp_path):
    env_file = tmp_path / "fresh.env"
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    assert env_file.is_file()
    assert bootstrap.load_env_file(env_file)["HEALTHMES_HERMES_WEBHOOK_SECRET"]


# ---------------------------------------------------------------------------
# Existing-config merge
# ---------------------------------------------------------------------------


def test_existing_config_is_merged_not_clobbered(bootstrap, hermes_home, env_file):
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"default": "user-chosen-model"},
                "platforms": {"telegram": {"token": "stale-token"}},
            }
        ),
        encoding="utf-8",
    )
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    # Unmanaged user keys survive; managed keys are overwritten.
    assert config["model"] == {"default": "user-chosen-model"}
    assert config["platforms"]["telegram"]["token"] == "123456:test-token"
    assert "mcp_servers" in config
    # The pre-merge file was backed up exactly once.
    backup = hermes_home / "config.yaml.healthmes-backup"
    assert backup.is_file()
    assert "stale-token" in backup.read_text()


# ---------------------------------------------------------------------------
# Docker mode
# ---------------------------------------------------------------------------


def test_docker_mode_defaults(bootstrap, hermes_home, env_file):
    assert run_bootstrap(bootstrap, hermes_home, env_file, "--mode", "docker") == 0

    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    servers = config["mcp_servers"]
    # In-cluster endpoints and the compose mount points from docker-compose.yml.
    assert servers["healthmes"]["url"] == "http://healthmes:8100/mcp"
    ow = servers["open_wearables"]
    assert ow["env"]["OPEN_WEARABLES_API_URL"] == "http://ow-backend:8000"
    assert "/opt/vendor/open-wearables-mcp" in ow["args"]
    assert ow["env"]["UV_PROJECT_ENVIRONMENT"] == "/opt/data/ow-mcp-venv"

    # Skills are copied into $HERMES_HOME/skills, which is the bind mount
    # (./data/hermes) the hermes container sees — same layout as native mode.
    dest = hermes_home / "skills" / "healthmes-planner"
    assert dest.is_dir() and not dest.is_symlink()
    assert (dest / "SKILL.md").is_file()

    # The snapshot sidecar carries the in-cluster healthmes endpoint; the
    # script itself stays byte-identical to the repo copy (URL via sidecar,
    # never hardcoded — the hard localhost-default rule).
    sidecar = yaml.safe_load((hermes_home / "scripts" / "healthmes_snapshot.json").read_text())
    assert sidecar == {"base_url": "http://healthmes:8100"}


def test_env_overrides_beat_mode_defaults(bootstrap, hermes_home, env_file, monkeypatch):
    monkeypatch.setenv("HEALTHMES_MCP_URL", "http://127.0.0.1:9999/mcp")
    monkeypatch.setenv("OW_BASE_URL", "http://127.0.0.1:8811")
    assert run_bootstrap(bootstrap, hermes_home, env_file, "--mode", "docker") == 0
    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert config["mcp_servers"]["healthmes"]["url"] == "http://127.0.0.1:9999/mcp"
    ow_env = config["mcp_servers"]["open_wearables"]["env"]
    assert ow_env["OPEN_WEARABLES_API_URL"] == "http://127.0.0.1:8811"


def test_api_token_flows_into_mcp_headers_and_sidecar(
    bootstrap, hermes_home, env_file, monkeypatch
):
    """With HEALTHMES_API_TOKEN set, the agent side must keep working: the
    rendered MCP registration carries the bearer header (mcp_tool.py url
    transports support `headers:`) and the briefing snapshot sidecar carries
    the token for its REST fetches."""
    monkeypatch.setenv("HEALTHMES_API_TOKEN", "glue-token")
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    healthmes = config["mcp_servers"]["healthmes"]
    assert healthmes["headers"] == {"Authorization": "Bearer glue-token"}

    sidecar = yaml.safe_load(
        (hermes_home / "scripts" / "healthmes_snapshot.json").read_text()
    )
    assert sidecar == {
        "base_url": "http://localhost:8100",
        "api_token": "glue-token",
    }


# ---------------------------------------------------------------------------
# Skill discovery / copy repair
# ---------------------------------------------------------------------------


def test_repo_skills_are_discovered(bootstrap):
    names = [path.name for path in bootstrap.discover_skill_dirs(REPO_ROOT)]
    assert names == [
        "doctor-visit-summary",
        "healthmes-capture",
        "healthmes-planner",
        "healthmes-sleep",
    ]


def test_legacy_symlink_is_migrated_to_copy(bootstrap, hermes_home, env_file, tmp_path):
    """Symlinks left by earlier bootstrap versions become real copies."""
    skills_home = hermes_home / "skills"
    skills_home.mkdir(parents=True)
    stale_target = tmp_path / "elsewhere"
    stale_target.mkdir()
    (skills_home / "healthmes-planner").symlink_to(stale_target)

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    dest = skills_home / "healthmes-planner"
    assert dest.is_dir() and not dest.is_symlink()
    assert (dest / "SKILL.md").read_text() == (
        REPO_ROOT / "skills" / "healthmes-planner" / "SKILL.md"
    ).read_text()


def test_drifted_skill_copy_is_resynced(bootstrap, hermes_home, env_file):
    """An edited installed copy is resynced from the repo on re-run."""
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    dest = hermes_home / "skills" / "healthmes-planner"
    (dest / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    (dest / "stale-extra.md").write_text("leftover\n", encoding="utf-8")

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    assert (dest / "SKILL.md").read_text() == (
        REPO_ROOT / "skills" / "healthmes-planner" / "SKILL.md"
    ).read_text()
    assert not (dest / "stale-extra.md").exists()


# ---------------------------------------------------------------------------
# Telegram delivery-target warning
# ---------------------------------------------------------------------------


def test_missing_home_chat_id_warns_about_delivery(bootstrap, hermes_home, env_file, capsys):
    """Without TELEGRAM_HOME_CHAT_ID the rendered config has neither a
    telegram home_channel nor deliver_extra.chat_id, so `deliver: telegram`
    fails at send time (vendor gateway 'No chat_id or home channel') — the
    bootstrap must say so instead of staying silent."""
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    err = capsys.readouterr().err
    assert "TELEGRAM_HOME_CHAT_ID" in err
    assert "/sethome" in err


def test_home_chat_id_set_does_not_warn(bootstrap, hermes_home, env_file, capsys):
    env_file.write_text(
        env_file.read_text() + "TELEGRAM_HOME_CHAT_ID=987654321\n", encoding="utf-8"
    )
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    assert "TELEGRAM_HOME_CHAT_ID" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Hermes-timezone-aware fallback clock (vendor hermes_time.py parity)
# ---------------------------------------------------------------------------


def test_hermes_now_honors_env_timezone(bootstrap, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Seoul")  # fixed +09:00, no DST
    now = bootstrap._hermes_now(tmp_path)
    assert now.utcoffset() == timedelta(hours=9)


def test_hermes_now_honors_config_timezone_key(bootstrap, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    (tmp_path / "config.yaml").write_text("timezone: Asia/Kolkata\n", encoding="utf-8")
    now = bootstrap._hermes_now(tmp_path)
    assert now.utcoffset() == timedelta(hours=5, minutes=30)  # fixed +05:30


def test_hermes_now_env_beats_config_and_bad_values_fall_back(
    bootstrap, tmp_path, monkeypatch
):
    (tmp_path / "config.yaml").write_text("timezone: Asia/Kolkata\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Seoul")
    assert bootstrap._hermes_now(tmp_path).utcoffset() == timedelta(hours=9)

    # Invalid names fall back to server-local time, never crash (vendor
    # hermes_time._get_zoneinfo behavior).
    monkeypatch.setenv("HERMES_TIMEZONE", "Not/AZone")
    (tmp_path / "config.yaml").unlink()
    now = bootstrap._hermes_now(tmp_path)
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.now().astimezone().utcoffset()


def test_payload_fallback_jobs_use_hermes_timezone(
    bootstrap, hermes_home, env_file, monkeypatch, capsys
):
    """Forcing the no-croniter fallback path: created_at/next_run_at must be
    stamped in the configured Hermes timezone (as vendor create_job does via
    hermes_time.now()), so the first briefing fires on the right wall clock."""
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Seoul")
    monkeypatch.setattr(bootstrap, "_import_vendor_cron_jobs", lambda home: None)

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    assert "payload-fallback" in capsys.readouterr().out

    jobs_doc = yaml.safe_load((hermes_home / "cron" / "jobs.json").read_text())
    jobs = {job["name"]: job for job in jobs_doc["jobs"]}
    assert len(jobs) == 3
    for job in jobs.values():
        for key in ("created_at", "next_run_at"):
            assert datetime.fromisoformat(job[key]).utcoffset() == timedelta(hours=9)
    # The wall-clock hour of the first run matches the cron expression in
    # the configured zone (07:00 KST, not 07:00 system-local).
    morning_next = datetime.fromisoformat(jobs["healthmes-morning-plan"]["next_run_at"])
    assert (morning_next.hour, morning_next.minute) == (7, 0)


# ---------------------------------------------------------------------------
# Fallback cron next-run computation
# ---------------------------------------------------------------------------


def test_next_cron_run_daily_and_weekly(bootstrap):
    # 2026-07-08 is a Wednesday.
    now = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)
    assert bootstrap._next_cron_run("0 7 * * *", now) == datetime(
        2026, 7, 9, 7, 0, tzinfo=UTC
    )
    assert bootstrap._next_cron_run("30 21 * * *", now) == datetime(
        2026, 7, 8, 21, 30, tzinfo=UTC
    )
    # cron weekday 0 = Sunday -> 2026-07-12.
    assert bootstrap._next_cron_run("0 18 * * 0", now) == datetime(
        2026, 7, 12, 18, 0, tzinfo=UTC
    )
    # A weekly schedule whose slot already passed today rolls a full week.
    sunday_evening = datetime(2026, 7, 12, 19, 0, tzinfo=UTC)
    assert bootstrap._next_cron_run("0 18 * * 0", sunday_evening) == datetime(
        2026, 7, 19, 18, 0, tzinfo=UTC
    )
    with pytest.raises(ValueError):
        bootstrap._next_cron_run("*/5 * * * *", now)
