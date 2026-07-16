"""Application settings.

All configuration is read from ``HEALTHMES_``-prefixed environment variables
(optionally via a repo-root ``.env`` file). Style follows the pydantic-settings
pattern used by ``vendor/open-wearables/mcp/app/config.py``.

Defaults target the mac-native run path (localhost, zero-setup sqlite);
docker-compose.yml injects in-cluster values via container environment.
Never hardcode docker service hostnames here.
"""

import datetime
import ipaddress
import logging
import zoneinfo
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """HealthMes service configuration."""

    model_config = SettingsConfigDict(
        env_prefix="HEALTHMES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="sqlite:///./data/healthmes.db",
        description="SQLAlchemy URL of the dedicated healthmes database. "
        "Defaults to a repo-local sqlite file for zero-setup native dev; "
        "point it at the dedicated postgres database for the full stack "
        "(see .env.example).",
    )
    ow_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the open-wearables backend REST API (read-only consumer).",
    )
    ow_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key for the open-wearables backend (same key the vendor MCP server uses).",
    )
    ow_user_id: str | None = Field(
        default=None,
        description="UUID of the single open-wearables user this deployment reads; "
        "when unset, tools fall back to the HEALTHMES_OW_USER_ID env var and then "
        "to auto-discovery via GET /api/v1/users (works only when the API key "
        "sees exactly one user).",
    )
    hermes_webhook_url: str = Field(
        default="http://localhost:8644/webhooks/healthmes-alerts",
        description="Hermes gateway webhook route URL for proactive alert pushes. "
        "Port 8644 is DEFAULT_PORT in vendor/hermes-agent/gateway/platforms/"
        "webhook.py; the path is /webhooks/{route_name} with route "
        "'healthmes-alerts' (config/hermes-config.yaml.tmpl).",
    )
    hermes_webhook_secret: SecretStr = Field(
        default=SecretStr(""),
        description="HMAC secret shared with the Hermes webhook route.",
    )
    public_base_url: str = Field(
        default="http://localhost:8100",
        description="Externally reachable base URL of this service, used to build "
        "decision-viewer links embedded in alerts (e.g. {public_base_url}/decisions/{id}).",
    )
    data_dir: Path = Field(
        default=Path("data"),
        description="Local-first data directory (media files, sqlite db, exports, "
        "pidfiles). Only paths are stored in the database.",
    )
    port: int = Field(
        default=8100,
        description="TCP port the HealthMes FastAPI service listens on.",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Interface uvicorn binds. The localhost-native default keeps "
        "the surface (medical records, health context, /mcp) off the network; "
        "set 0.0.0.0 for LAN/compose deployments — `healthmes serve` then "
        "refuses to start unless api_token is configured (docs/PLAN.md §9).",
    )
    api_token: SecretStr = Field(
        default=SecretStr(""),
        description="Bearer token protecting the whole HTTP surface (REST + "
        "/mcp). When set, every request must send 'Authorization: Bearer "
        "<token>' (the Android collector already does); decision-viewer pages "
        "additionally accept a derived read-only ?token= link credential. "
        "Empty disables auth — acceptable only on a loopback bind.",
    )
    scheduler_enabled: bool = Field(
        default=False,
        description="Enable the in-process APScheduler loops (10-minute trigger "
        "sweep, hourly cognitive-energy persist, weekly backup). Keep disabled "
        "in tests and one-off tooling.",
    )
    timezone: str | None = Field(
        default=None,
        description="IANA timezone of the user (e.g. 'Asia/Seoul'); local-day "
        "boundaries and the calendar/app-usage joins of the tranche-2 MCP tools "
        "use it. None = the machine's local timezone (right on mac-native; "
        "docker containers run UTC clocks, so compose forwards HEALTHMES_TIMEZONE).",
    )

    # Delivery: proactive alerts reach the user through the Hermes webhook
    # (phone+watch via Telegram) AND/OR the native companion apps, which poll
    # /v1/alerts + /v1/briefing/glance. With native delivery on, a fired
    # trigger is surfaced to the apps even when no Hermes webhook is
    # configured or its push fails — so the phone gets alerts without Telegram.
    native_alert_delivery: bool = Field(
        default=False,
        description="Surface fired triggers to the native companion apps "
        "(/v1/alerts + glance) regardless of the Hermes webhook outcome — "
        "enables phone/watch alerts without Telegram. Alert hygiene (quiet "
        "hours, cooldown, daily budget, dedup) still applies.",
    )

    # Native capture uploads (issue #10 companion apps; healthmes/api/media.py).
    media_max_upload_bytes: int = Field(
        default=15 * 1024 * 1024,
        ge=1,
        description="Maximum accepted size in bytes of one media upload "
        "(POST /v1/media). Uploads beyond the cap are rejected with 413 and "
        "nothing is stored. Default 15 MiB — plenty for phone photos and "
        "voice memos while keeping a LAN peer from filling the disk.",
    )

    # Alert hygiene (docs/PLAN.md §11: a noisy assistant gets muted within a
    # week). Consumed by healthmes/engine/triggers.py before any webhook push.
    quiet_hours_start: datetime.time = Field(
        default=datetime.time(22, 30),
        description="Start of the do-not-disturb window (local time, e.g. '22:30'). "
        "No proactive alerts are pushed inside the window.",
    )
    quiet_hours_end: datetime.time = Field(
        default=datetime.time(7, 0),
        description="End of the do-not-disturb window (local time, e.g. '07:00').",
    )
    alert_daily_budget: int = Field(
        default=8,
        ge=0,
        description="Maximum proactive alerts per calendar day across all trigger "
        "rules; further trigger firings are recorded but not pushed.",
    )
    alert_cooldown_minutes: int = Field(
        default=60,
        ge=0,
        description="Minimum minutes between two pushes of the same trigger rule.",
    )

    # Calendar mirror backends (docs/PLAN.md §6). Both disabled by default:
    # they need real credentials (Google OAuth client secret + interactive
    # bootstrap; iCloud app-specific password) that tests must never require.
    google_calendar_enabled: bool = Field(
        default=False,
        description="Enable the Google Calendar mirror backend. Requires the OAuth "
        "installed-app bootstrap: client secret at {data_dir}/google/"
        "client_secret.json, token minted interactively into {data_dir}/google/"
        "calendar_token.json (healthmes/calendars/google.py).",
    )
    google_calendar_id: str = Field(
        default="primary",
        description="Google calendar id to mirror ('primary' or a specific calendar's id).",
    )
    google_client_secret_file: Path | None = Field(
        default=None,
        description="Optional path to a Google OAuth client-secret JSON, used by "
        "`healthmes connect google` when {data_dir}/google/client_secret.json "
        "is absent (the standard location keeps working; this is an override "
        "for keeping the download wherever you like).",
    )
    google_poll_minutes: int = Field(
        default=5,
        ge=1,
        description="Polling interval for Google Calendar incremental sync "
        "(syncToken; docs/PLAN.md §6 says 5 minutes).",
    )
    caldav_enabled: bool = Field(
        default=False,
        description="Enable the iCloud CalDAV mirror backend "
        "(healthmes/calendars/caldav_icloud.py).",
    )
    caldav_url: str = Field(
        default="https://caldav.icloud.com",
        description="CalDAV principal discovery URL (iCloud default; any RFC 4791 server works).",
    )
    caldav_username: str = Field(
        default="",
        description="CalDAV username (for iCloud: the Apple ID email).",
    )
    caldav_app_password: SecretStr = Field(
        default=SecretStr(""),
        description="App-specific password for CalDAV "
        "(https://appleid.apple.com for iCloud; never the account password).",
    )
    caldav_calendar_name: str | None = Field(
        default=None,
        description="Display name of the CalDAV calendar to mirror; None picks "
        "the principal's default calendar.",
    )
    caldav_poll_minutes: int = Field(
        default=10,
        ge=1,
        description="Polling interval for CalDAV sync (ctag short-circuit; "
        "docs/PLAN.md §6 says 10 minutes).",
    )

    # Local-first encrypted backups (docs/PLAN.md §9; healthmes/backup/).
    backup_dir: Path | None = Field(
        default=None,
        description="Directory local snapshots are written to; None means {data_dir}/backups.",
    )
    backup_passphrase: SecretStr = Field(
        default=SecretStr(""),
        description="Passphrase snapshots are age-encrypted with (scrypt-derived). "
        "Empty: `healthmes backup create` errors and the weekly backup job "
        "skips with a warning.",
    )
    ow_database_url: str | None = Field(
        default=None,
        description="Direct SQLAlchemy/postgres URL of the open-wearables database, "
        "used only to include its pg_dump in snapshots (the REST ow_base_url "
        "cannot produce a dump). None skips that snapshot section.",
    )
    hermes_home: Path | None = Field(
        default=None,
        description="Hermes agent home directory (memory/state) to include in "
        "snapshots. None falls back to the vendor HERMES_HOME env var; unset "
        "skips the section.",
    )

    # Remote vault replication of encrypted snapshots (docs/PLAN.md §9 business
    # seam; healthmes/backup/remote_vault.py). Resolution in the backup module
    # is attribute-first with HEALTHMES_VAULT_* env fallback, so these typed
    # fields are optional sugar — the module works from env vars alone. The
    # None defaults keep the env fallback reachable for Settings objects built
    # before the variables were set (tests construct Settings early).
    backup_provider: str | None = Field(
        default=None,
        description="Backup provider selector: 'local' (default when unset) or "
        "'remote_vault' ('remote' is accepted as an alias). remote_vault keeps "
        "the local snapshot AND replicates the age-encrypted envelope to the "
        "S3-compatible vault below; the weekly job follows the same selector.",
    )
    vault_endpoint: str | None = Field(
        default=None,
        description="S3-compatible endpoint URL of the remote vault (R2/MinIO). "
        "None means AWS S3 proper (region-derived endpoint).",
    )
    vault_bucket: str | None = Field(
        default=None,
        description="Bucket the encrypted snapshot envelopes are replicated to. "
        "Unset disables the remote vault (provider 'remote_vault' then errors "
        "with a pointer to this setting).",
    )
    vault_access_key_id: str | None = Field(
        default=None,
        description="Vault access key id; unset falls back to the boto3 default "
        "credential chain (env vars, shared config, instance roles).",
    )
    vault_secret_access_key: SecretStr = Field(
        default=SecretStr(""),
        description="Vault secret access key (paired with vault_access_key_id).",
    )
    vault_region: str | None = Field(
        default=None,
        description="Vault region (e.g. 'us-east-1'; R2 uses 'auto').",
    )
    vault_prefix: str | None = Field(
        default=None,
        description="Key prefix inside the bucket snapshots live under; "
        "None uses the module default ('healthmes-vault', "
        "healthmes/backup/remote_vault.py::DEFAULT_VAULT_PREFIX).",
    )

    @field_validator(
        "ow_user_id",
        "timezone",
        "backup_dir",
        "ow_database_url",
        "hermes_home",
        "google_client_secret_file",
        "backup_provider",
        "vault_endpoint",
        "vault_bucket",
        "vault_access_key_id",
        "vault_region",
        "vault_prefix",
        mode="before",
    )
    @classmethod
    def _blank_env_is_none(cls, value: object) -> object:
        """Treat blank env vars as unset for the optional fields.

        ``HEALTHMES_TIMEZONE=`` (empty) must behave like the variable being
        absent — docker-compose forwards optional vars as empty strings, and
        ``Path("")`` would otherwise silently become ``Path(".")``.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (env-derived, cached)."""
    return Settings()


def system_timezone() -> datetime.tzinfo:
    """Return the machine's IANA timezone, never a captured local offset."""
    try:
        from tzlocal import get_localzone

        local = get_localzone()
        if isinstance(local, zoneinfo.ZoneInfo):
            return local
        key = getattr(local, "key", None)
        if key:
            return zoneinfo.ZoneInfo(str(key))
    except Exception:
        logger.debug("tzlocal could not resolve the system timezone", exc_info=True)

    try:
        target = Path("/etc/localtime").resolve(strict=True)
        parts = target.parts
        zoneinfo_index = parts.index("zoneinfo")
        name = "/".join(parts[zoneinfo_index + 1 :])
        if name:
            return zoneinfo.ZoneInfo(name)
    except (OSError, ValueError, zoneinfo.ZoneInfoNotFoundError):
        logger.debug("/etc/localtime did not resolve to an IANA timezone", exc_info=True)

    logger.warning("Could not resolve the system IANA timezone; falling back to UTC")
    return datetime.UTC


def resolve_timezone(settings: Settings) -> datetime.tzinfo:
    """The user's local timezone: ``Settings.timezone`` (IANA) or the machine's.

    A configured-but-invalid name raises ``ZoneInfoNotFoundError`` — loud,
    never a silent UTC fallback (silent guessing corrupts every local-day
    join, quiet-hours window and dedup key). ``None`` (unset) means the
    machine's local timezone: right on mac-native, where machine tz == user
    tz; docker deployments forward ``HEALTHMES_TIMEZONE`` because container
    clocks run UTC.
    """
    name = getattr(settings, "timezone", None)
    if name:
        return zoneinfo.ZoneInfo(str(name))
    return system_timezone()


def is_loopback_host(host: str) -> bool:
    """True when ``host`` is a loopback bind (localhost / 127.x / ::1).

    Non-IP hostnames other than ``localhost`` count as non-loopback — the
    safe direction for the serve-time auth interlock.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
