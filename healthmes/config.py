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
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from healthmes.timezones import parse_timezone

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
    decision_hermes_base_url: str | None = Field(
        default=None,
        max_length=2_048,
        description="Hermes origin exposing the HealthMes-required "
        "single-model-iteration contract. None keeps the decision REST "
        "entrypoint fail-closed; the generic Hermes chat endpoint is never "
        "used as a fallback.",
    )
    decision_hermes_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Optional bearer credential for the dedicated Hermes "
        "decision runtime. Remote origins require a credential.",
    )
    decision_hermes_model: str | None = Field(
        default=None,
        max_length=128,
        description="Exact model identity expected from Hermes decision "
        "iterations. Required together with decision_hermes_provider and "
        "decision_hermes_base_url.",
    )
    decision_hermes_provider: str | None = Field(
        default=None,
        max_length=128,
        description="Exact provider identity expected from Hermes decision "
        "iterations. Required together with decision_hermes_model and "
        "decision_hermes_base_url.",
    )
    decision_hermes_discovery_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        description="Timeout for probing the Hermes single-iteration "
        "capability contract.",
    )
    decision_hermes_max_iteration_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=300,
        description="Upper bound for one Hermes model iteration.",
    )
    decision_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=300,
        description="Wall-clock deadline for one complete HealthMes decision "
        "turn, including all model iterations and context calls.",
    )
    decision_finalization_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        description="Maximum time allowed for atomic source revalidation "
        "and DecisionRecord persistence after model reasoning completes. "
        "Bounds process, SQLite file, and PostgreSQL transaction waits so "
        "accepted requests can drain during shutdown.",
    )
    decision_execution_scope: Literal["local", "hosted"] = Field(
        default="local",
        description="Where decision prompts and aggregate context are "
        "processed. Local requires a loopback Hermes origin. Operators must "
        "explicitly select hosted when loopback Hermes proxies a cloud model.",
    )
    decision_max_pending_requests: int = Field(
        default=8,
        ge=1,
        le=128,
        description="Maximum accepted Decision Agent requests, including the "
        "one currently executing. Additional requests fail fast with 429.",
    )
    decision_owner_principal_id: str = Field(
        default="owner",
        min_length=1,
        max_length=255,
        description="Server-owned identity of the single local HealthMes "
        "owner. Decision clients cannot supply or override this value.",
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
    calendar_adjustment_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Dedicated secret for one-time calendar adjustment confirmation handles. "
        "Generate and persist a high-entropy value with scripts/bootstrap.py; "
        "calendar adjustment tools fail closed when it is missing or too short.",
    )
    telegram_owner_user_id: str = Field(
        default="",
        description="Telegram user id of the single owner allowed to confirm "
        "calendar mutations. Empty or '*' makes confirmation fail closed.",
    )
    telegram_owner_chat_id: str = Field(
        default="",
        description="Telegram chat id in which the owner may confirm calendar "
        "mutations. Empty or '*' makes confirmation fail closed.",
    )
    scheduler_enabled: bool = Field(
        default=False,
        description="Enable the in-process APScheduler loops (10-minute trigger "
        "sweep, hourly cognitive-energy persist, weekly backup, and enabled "
        "provider collectors). Keep disabled in tests and one-off tooling.",
    )
    activitywatch_enabled: bool = Field(
        default=False,
        description="Enable periodic import from the loopback ActivityWatch "
        "server. The global scheduler_enabled gate must also be enabled.",
    )
    activitywatch_interval_minutes: int = Field(
        default=5,
        ge=1,
        le=24 * 60,
        description="Minutes between ActivityWatch imports.",
    )
    activitywatch_device_id: str = Field(
        default="activitywatch-desktop",
        min_length=1,
        max_length=255,
        description="Stable HealthMes device identifier for this ActivityWatch collector. "
        "Set a unique value on every computer that syncs to one HealthMes store.",
    )
    activitywatch_platform: Literal["macos", "windows", "linux"] = Field(
        default="macos",
        description="Desktop platform represented by this ActivityWatch collector.",
    )
    activitywatch_timezone: str | None = Field(
        default=None,
        max_length=64,
        description="IANA timezone or UTC fixed offset used to bucket ActivityWatch "
        "events. None inherits the HealthMes user timezone.",
    )
    activitywatch_base_url: str = Field(
        default="http://127.0.0.1:5600",
        description="Loopback-only ActivityWatch REST API base URL.",
    )
    activitywatch_window_minutes: int = Field(
        default=24 * 60,
        ge=1,
        le=7 * 24 * 60,
        description="Initial ActivityWatch lookback when no cursor exists. "
        "Later runs resume incrementally from the stored cursor.",
    )
    activitywatch_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=300,
        description="Bounded timeout for each ActivityWatch HTTP operation.",
    )
    activitywatch_window_bucket_id: str | None = Field(
        default=None,
        max_length=255,
        description="Optional explicit ActivityWatch current-window bucket id.",
    )
    activitywatch_afk_bucket_id: str | None = Field(
        default=None,
        max_length=255,
        description="Optional explicit ActivityWatch AFK-status bucket id.",
    )
    timezone: str | None = Field(
        default=None,
        description="IANA timezone or UTC fixed offset of the user "
        "(e.g. 'Asia/Seoul' or 'UTC+09:00'); local-day boundaries and the "
        "calendar/app-usage joins of the tranche-2 MCP tools use it. "
        "None = the machine's local timezone (right on mac-native; docker "
        "containers run UTC clocks, so compose forwards HEALTHMES_TIMEZONE).",
    )

    # Delivery: proactive alerts reach the user through the Hermes webhook
    # (phone+watch via Telegram) AND/OR the native companion apps, which poll
    # /v1/alerts + /v1/briefing/glance. With native delivery on, a fired
    # trigger is surfaced to the apps even when no Hermes webhook is
    # configured or its push fails — so the phone gets alerts without Telegram.
    native_alert_delivery: bool = Field(
        default=True,
        description="Surface fired triggers to the native companion apps "
        "(/v1/alerts + glance) regardless of the Hermes webhook outcome — "
        "enables phone/watch alerts without Telegram. Alert hygiene (quiet "
        "hours, cooldown, daily budget, dedup) still applies. On by default "
        "(PLAN §13: alerts must work with zero setup); set false to make "
        "Telegram the only channel.",
    )

    # Raw-first ingest receiver (PLAN §13; healthmes/api/ingest.py). Bridge
    # apps push batches of HealthKit samples; 64 MiB comfortably covers
    # multi-day backlogs while bounding request memory.
    ingest_max_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1,
        description="Maximum accepted /v1/ingest payload size in bytes.",
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
    nutrition_ollama_base_url: str = Field(
        default="http://127.0.0.1:11434",
        description="Ollama API used for local intake-photo extraction. "
        "Non-loopback endpoints require HTTPS and allow_remote_vision on each request.",
    )
    nutrition_vision_provider: Literal["ollama", "openai", "gemini", "anthropic", "xai"] = Field(
        default="ollama",
        description="Intake-photo vision provider. Remote providers still require "
        "allow_remote_vision=true on every analysis request.",
    )
    nutrition_vision_model: str = Field(
        default="qwen3-vl:4b-instruct",
        min_length=1,
        description="Pinned Ollama vision model tag; mutable latest aliases are not recommended.",
    )
    nutrition_vision_model_digest: str | None = Field(
        default=None,
        description="Optional immutable model digest recorded with every observation.",
    )
    nutrition_openai_base_url: str = Field(
        default="https://api.openai.com",
        description="OpenAI API origin used for remote intake-photo extraction.",
    )
    nutrition_openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="OpenAI API key for explicitly authorized remote photo analysis.",
    )
    nutrition_openai_model: str = Field(
        default="gpt-5.6-sol",
        min_length=1,
        description="OpenAI vision model recorded with each observation.",
    )
    nutrition_gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        description="Gemini API base URL used for remote intake-photo extraction.",
    )
    nutrition_gemini_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Gemini API key for explicitly authorized remote photo analysis.",
    )
    nutrition_gemini_model: str = Field(
        default="gemini-3.6-flash",
        min_length=1,
        description="Gemini vision model recorded with each observation.",
    )
    nutrition_anthropic_base_url: str = Field(
        default="https://api.anthropic.com",
        description="Anthropic API origin used for remote intake-photo extraction.",
    )
    nutrition_anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key for explicitly authorized remote photo analysis.",
    )
    nutrition_anthropic_model: str = Field(
        default="claude-fable-5",
        min_length=1,
        description="Claude vision model recorded with each observation.",
    )
    nutrition_xai_base_url: str = Field(
        default="https://api.x.ai",
        description="xAI API origin used for remote intake-photo extraction.",
    )
    nutrition_xai_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="xAI API key for explicitly authorized remote photo analysis.",
    )
    nutrition_xai_model: str = Field(
        default="grok-4.5",
        min_length=1,
        description="Grok vision model recorded with each observation.",
    )
    nutrition_vision_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Timeout for one structured photo-analysis request.",
    )
    nutrition_whisper_base_url: str = Field(
        default="http://127.0.0.1:8080",
        description="Loopback whisper.cpp server used to transcribe nutrition "
        "voice captures. Non-loopback transcription endpoints are rejected.",
    )
    nutrition_transcription_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Timeout for one local nutrition voice transcription.",
    )
    nutrition_transcription_language: str = Field(
        default="auto",
        min_length=1,
        max_length=32,
        description="Language passed to the local whisper.cpp transcription server.",
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
        "activitywatch_timezone",
        "activitywatch_window_bucket_id",
        "activitywatch_afk_bucket_id",
        "decision_hermes_base_url",
        "decision_hermes_model",
        "decision_hermes_provider",
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

    @field_validator(
        "decision_hermes_model",
        "decision_hermes_provider",
    )
    @classmethod
    def _strip_decision_runtime_identity(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("decision runtime identity must not be blank")
        return cleaned

    @field_validator("decision_owner_principal_id")
    @classmethod
    def _strip_decision_owner_principal_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(
                "decision owner principal ID must not be blank"
            )
        return cleaned

    @model_validator(mode="after")
    def _validate_decision_runtime_bundle(self) -> Self:
        configured = (
            self.decision_hermes_base_url,
            self.decision_hermes_model,
            self.decision_hermes_provider,
        )
        if any(value is not None for value in configured) and not all(
            value is not None for value in configured
        ):
            raise ValueError(
                "decision_hermes_base_url, decision_hermes_model, and "
                "decision_hermes_provider must be configured together"
            )
        if (
            self.decision_execution_scope == "local"
            and self.decision_hermes_base_url is not None
        ):
            parsed = urlparse(self.decision_hermes_base_url)
            if (
                parsed.hostname is not None
                and not is_loopback_host(parsed.hostname)
            ):
                raise ValueError(
                    "local decision execution requires a loopback Hermes "
                    "origin; set decision_execution_scope='hosted' for "
                    "remote or cloud processing"
                )
        return self

    @field_validator("activitywatch_device_id")
    @classmethod
    def _validate_activitywatch_device_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("activitywatch_device_id must not be blank")
        return value

    @field_validator(
        "activitywatch_window_bucket_id",
        "activitywatch_afk_bucket_id",
    )
    @classmethod
    def _validate_activitywatch_bucket_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or value in {".", ".."}:
            raise ValueError("ActivityWatch bucket IDs must be non-dot path segments")
        return value

    @field_validator("activitywatch_timezone")
    @classmethod
    def _validate_activitywatch_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parse_timezone(value)
        except ValueError as exc:
            raise ValueError(
                "activitywatch_timezone must be a valid IANA name or UTC offset"
            ) from exc
        return value

    @field_validator("activitywatch_base_url")
    @classmethod
    def _validate_activitywatch_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "http" or parsed.hostname is None:
            raise ValueError("activitywatch_base_url must use loopback HTTP")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("activitywatch_base_url must not contain credentials")
        host = parsed.hostname
        if host != "localhost":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    raise ValueError(
                        "activitywatch_base_url must be loopback-only"
                    )
            except ValueError as exc:
                if "loopback-only" in str(exc):
                    raise
                raise ValueError(
                    "activitywatch_base_url must be loopback-only"
                ) from exc
        return value.rstrip("/")


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
    """The user's configured timezone or the machine's local IANA timezone.

    IANA names and stable ``UTC+09:00`` fixed offsets are accepted. A
    configured-but-invalid value raises ``ZoneInfoNotFoundError`` for backward
    compatibility, never a silent UTC fallback. ``None`` means the machine's
    local timezone.
    """
    name = getattr(settings, "timezone", None)
    if name:
        try:
            return parse_timezone(str(name))
        except ValueError as exc:
            raise zoneinfo.ZoneInfoNotFoundError(str(name)) from exc
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
