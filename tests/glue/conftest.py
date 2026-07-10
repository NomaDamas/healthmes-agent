"""Fixtures for the agent-plane glue tests (template, bootstrap, cron).

No network, no Docker, no real credentials: everything runs against tmp
directories and the vendored sources on disk.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_HERMES = REPO_ROOT / "vendor" / "hermes-agent"

# Env vars that feed bootstrap's template context / path resolution. Cleared
# for every test so the developer's shell can never leak into assertions.
_BOOTSTRAP_ENV_KEYS = (
    "HERMES_HOME",
    "HERMES_TIMEZONE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_HOME_CHAT_ID",
    "TELEGRAM_HOME_CHAT_NAME",
    "TELEGRAM_ALLOWED_USER_IDS",
    "HERMES_WEBHOOK_PORT",
    "HERMES_MODEL",
    "HERMES_PROVIDER",
    "HERMES_MODEL_BASE_URL",
    "HEALTHMES_BASE_URL",
    "HEALTHMES_API_TOKEN",
    "HEALTHMES_HERMES_WEBHOOK_SECRET",
    "HEALTHMES_ALERT_PROMPT",
    "HEALTHMES_OW_BASE_URL",
    "HEALTHMES_OW_API_KEY",
    "HEALTHMES_MCP_URL",
    "HEALTHMES_PORT",
    "OPEN_WEARABLES_API_KEY",
    "OW_BASE_URL",
    "OW_MCP_DIR",
    "OW_MCP_VENV_DIR",
    "OW_MCP_UV_CACHE_DIR",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every bootstrap-relevant variable from the process env."""
    for key in _BOOTSTRAP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session")
def bootstrap():
    """The scripts/bootstrap.py module, imported from its file path."""
    spec = importlib.util.spec_from_file_location(
        "healthmes_bootstrap", REPO_ROOT / "scripts" / "bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec: the @dataclass decorator resolves the module via
    # sys.modules when `from __future__ import annotations` is in effect.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def snapshot_script():
    """scripts/healthmes_briefing_snapshot.py, imported from its file path."""
    spec = importlib.util.spec_from_file_location(
        "healthmes_briefing_snapshot",
        REPO_ROOT / "scripts" / "healthmes_briefing_snapshot.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def vendor_cron(tmp_path_factory: pytest.TempPathFactory):
    """Vendor cron.jobs imported once, bound to a session-scoped tmp home.

    cron/jobs.py resolves HERMES_DIR from the HERMES_HOME env var at import
    time, so the module is (re-)imported with the env var pointing at the
    tmp home and any previously-cached copy purged first.
    """
    home = tmp_path_factory.mktemp("vendor-hermes-home")
    previous_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    vendor_path = str(VENDOR_HERMES)
    inserted = vendor_path not in sys.path
    if inserted:
        sys.path.insert(0, vendor_path)
    for name in [m for m in sys.modules if m == "cron" or m.startswith("cron.")]:
        del sys.modules[name]
    try:
        import cron.jobs as vendor_jobs  # type: ignore[import-not-found]

        assert Path(vendor_jobs.JOBS_FILE).parent.parent == home.resolve()
        yield vendor_jobs, home
    finally:
        if inserted:
            try:
                sys.path.remove(vendor_path)
            except ValueError:
                pass
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home
