"""Personal Data Node storage control plane.

The database owns policy and audit state; payload bytes remain below
``HEALTHMES_DATA_DIR``. Maintenance is deliberately idempotent and path-safe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from healthmes import clock
from healthmes.activity.locking import (
    activity_write_lock,
    global_write_plane_guard,
    lock_activity_write_plane,
    session_holds_write_plane,
)
from healthmes.calendar_retention import (
    purge_expired_calendar_mirrors,
)
from healthmes.config import Settings
from healthmes.durable_files import (
    DurableFileIdentity,
    MaintenanceBudget,
    MaintenanceBudgetExceeded,
    durable_unlink,
    open_directory_anchored,
    read_directory_batch,
    recover_durable_unlink_target,
    require_directory_entry_durability,
)
from healthmes.engine.alert_visibility import (
    expire_trigger_event_answers,
    lock_trigger_events_for_retention,
)
from healthmes.storage.staging import reconcile_staging_files
from healthmes.store import (
    AppUsageSample,
    DecisionRecord,
    FoodLog,
    MedicalRecord,
    PurgeJob,
    RawIngestEvent,
    RetentionPolicy,
    StorageObject,
    StorageUsageDaily,
    WellnessEvent,
    get_engine,
)
from healthmes.store.decision_receipts import (
    purge_expired_decision_receipts,
    scrub_decision_receipt_results,
)
from healthmes.store.session import session_scope

logger = logging.getLogger(__name__)

_FILE_CLEANUP_IDENTITY_VERSION = 2
_LEGACY_FILE_CLEANUP_IDENTITY_VERSION = 1
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_QUARANTINE_ENTRY = "payload"
_CLEANUP_QUARANTINE_PREFIX = ".healthmes-storage-delete-"
_CLEANUP_JOURNAL_PREFIX = ".healthmes-storage-cleanup-v1-"
_CLEANUP_JOURNAL_DIRECTORY = (
    "raw_ingest/.healthmes-storage-delete-journal"
)
_CLEANUP_JOURNAL_MAX_BYTES = 16 * 1024
_CLEANUP_JOURNAL_SCAN_LIMIT = 256
_CLEANUP_JOURNAL_CURSOR_NAME = (
    ".healthmes-storage-cleanup-scan-cursor-v1.json"
)
_CLEANUP_JOURNAL_CURSOR_MAX_BYTES = 1024
_MAINTENANCE_DISCOVERY_ENTRY_LIMIT = 1024
_MAINTENANCE_DISCOVERY_MAX_SECONDS = 1.0
_MAINTENANCE_TIMEOUT_SECONDS = 10.0
_MAINTENANCE_MAX_HASH_BYTES = 256 * 1024 * 1024
_MAINTENANCE_MAX_DIRECTORY_ENTRIES = 4096
_USAGE_SCAN_ENTRY_LIMIT = 100_000
_USAGE_SCAN_MAX_SECONDS = 2.0
_DISCOVERY_CURSOR_NAME = ".healthmes-unindexed-discovery-v2.json"
_DISCOVERY_CURSOR_MAX_BYTES = 128 * 1024
_DISCOVERY_CLASS_QUANTUM = 32
_DISCOVERY_MAX_DEPTH = 512
_DISCOVERY_ROOTS = (
    ("media", "media"),
    ("raw_ingest", "raw_payload"),
)
_MAINTENANCE_NEW_OBJECT_LIMIT = 128
_MAINTENANCE_RETRY_OBJECT_LIMIT = 128
_CLEANUP_MANUAL_REVIEW_REASONS = frozenset(
    {
        "cleanup_outcome_unproven",
        "unknown_hard_links_after_cleanup",
    }
)
_CLEANUP_JOURNAL_NAME = re.compile(
    rf"^{re.escape(_CLEANUP_JOURNAL_PREFIX)}"
    r"(?P<object_id>[0-9a-f]{32})-"
    r"(?P<state>intent|progress|complete|manual-review)\.json$"
)
_CLEANUP_JOURNAL_TEMP_NAME = re.compile(
    rf"^{re.escape(_CLEANUP_JOURNAL_PREFIX)}"
    r"(?P<object_id>[0-9a-f]{32})-"
    r"(?P<state>intent|progress|complete|manual-review)\.json"
    r"\.tmp-[0-9a-f]{32}$"
)
_DURABLE_UNLINK_QUARANTINE_PREFIX = ".healthmes-unlink-"
_DURABLE_UNLINK_RECOVERY_DIRECTORY = ".healthmes-recovery"
_INTERNAL_STORAGE_CONTROL_NAMES = (
    _DISCOVERY_CURSOR_NAME,
    _CLEANUP_JOURNAL_CURSOR_NAME,
    ".healthmes-staging-fallback-cursor-v1.json",
    ".healthmes-staging-index-cursor-v1.json",
)
_CLEANUP_QUARANTINE_NAME = re.compile(
    rf"^{re.escape(_CLEANUP_QUARANTINE_PREFIX)}"
    r"[0-9a-f]{20}-[0-9a-f]{32}$"
)
_DURABLE_UNLINK_QUARANTINE_NAME = re.compile(
    rf"^{re.escape(_DURABLE_UNLINK_QUARANTINE_PREFIX)}"
    r"(?:v2-[0-9a-f]{32}|[0-9a-f]{32}-.+)$"
)
_INTERNAL_STORAGE_CONTROL_TEMP_NAME = re.compile(
    rf"^(?:{re.escape(_DISCOVERY_CURSOR_NAME)}|"
    rf"{re.escape(_CLEANUP_JOURNAL_CURSOR_NAME)}|"
    r"\.healthmes-staging-fallback-cursor-v1\.json|"
    r"\.healthmes-staging-index-cursor-v1\.json)"
    r"\.tmp-[0-9a-f]{32}$"
)

RETENTION_PRESETS: dict[str, int | None] = {
    "1d": 1,
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "90d": 90,
    "forever": None,
}
DEFAULT_RETENTION: dict[str, str] = {
    "raw_payload": "14d",
    "media": "7d",
    "nutrition_media": "7d",
    "nutrition_raw_capture": "14d",
    "normalized": "30d",
    "wearable_normalized": "30d",
    "nutrition_observation": "90d",
    "nutrition_confirmation": "forever",
    "aggregate": "forever",
    "decision": "forever",
    "alert": "7d",
    "medical_record": "forever",
    "activity_raw": "14d",
    "activity_hourly": "90d",
    "activity_daily": "forever",
    "calendar_mirror": "90d",
}


@dataclass(frozen=True, slots=True)
class StorageMaintenanceReport:
    job_id: str
    dry_run: bool
    candidates: int
    records_purged: int
    files_deleted: int
    file_cleanup_pending: int
    deleted: int
    bytes_reclaimed: int
    decision_candidates: int
    decisions_deleted: int
    decision_receipt_candidates: int
    decision_receipts_deleted: int
    budget_exhausted: bool
    budget_resource: str | None
    budget_phase: str | None
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StorageUsageSnapshot:
    provider: str
    measured_on: date | None
    usage: dict[str, dict[str, int]]


@dataclass(frozen=True, slots=True)
class _PendingFileCleanup:
    object_id: uuid.UUID
    relative_path: str
    size_bytes: int
    identity: dict[str, object]
    retry: bool


@dataclass(frozen=True, slots=True)
class _CleanupOutcome:
    bytes_reclaimed: int
    files_deleted: int


@dataclass(frozen=True, slots=True)
class _CleanupParent:
    path: Path
    name: str
    descriptor: int | None


@dataclass(frozen=True, slots=True)
class _CleanupQuarantine:
    path: Path
    name: str
    parent_descriptor: int | None
    descriptor: int | None


class _CleanupIdentityMismatch(RuntimeError):
    """A cleanup path now names a different payload generation."""


class _CleanupAncestorUnavailable(OSError):
    """The storage root or an intermediate cleanup directory is unavailable."""


class _DiscoveryAncestorMissing(OSError):
    """A discovery directory disappeared before its next bounded slice."""


class _DiscoveryUnsafeAncestor(OSError):
    """A discovery path is not a no-follow regular directory chain."""


@dataclass(slots=True)
class _DiscoveryFrame:
    component: str | None
    device: int | None = None
    inode: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    offset: int = 0
    batch_index: int = 0
    rescan: bool = False


@dataclass(slots=True)
class _DiscoveryState:
    next_class: str
    stacks: dict[str, list[_DiscoveryFrame]]


class _CleanupManualReviewRequired(_CleanupIdentityMismatch):
    """Cleanup cannot be acknowledged without an operator decision."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _CleanupGenerationOrphaned(_CleanupManualReviewRequired):
    """The inode survived only through an unknown hard link."""

    def __init__(self, message: str) -> None:
        super().__init__("unknown_hard_links_after_cleanup", message)


class _CleanupOutcomeUnproven(_CleanupManualReviewRequired):
    """A prior attempt removed every provable name without a completion mark."""

    def __init__(self, message: str) -> None:
        super().__init__("cleanup_outcome_unproven", message)


@dataclass(frozen=True, slots=True)
class _ManualReviewCleanup:
    candidate: _PendingFileCleanup
    reason: str


@dataclass(frozen=True, slots=True)
class _CleanupJournalState:
    intent_sha256: str
    guarded_generations: frozenset[tuple[int, int]]
    completed_generations: frozenset[tuple[int, int]]
    removed_generations: frozenset[tuple[int, int]]
    active_generation: tuple[int, int] | None
    complete: bool
    manual_review_reason: str | None


@dataclass(frozen=True, slots=True)
class _CleanupJournalScanCursor:
    directory_device: int
    directory_inode: int
    offset: int
    batch_index: int


@dataclass(frozen=True, slots=True)
class _StorageMaintenancePlan:
    job_id: uuid.UUID
    dry_run: bool
    candidates: int
    records_purged: int
    file_cleanup_pending: int
    decision_candidates: int
    decision_receipt_candidates: int
    budget_resource: str | None
    budget_phase: str | None
    precommit_errors: tuple[str, ...]
    file_cleanup: tuple[_PendingFileCleanup, ...]


@dataclass(slots=True)
class _MaintenanceBudgetStatus:
    resource: str | None = None
    phase: str | None = None

    def record(self, exc: MaintenanceBudgetExceeded) -> None:
        if self.resource is None:
            self.resource = exc.resource
            self.phase = exc.phase


def _storage_maintenance_budget(settings: Settings) -> MaintenanceBudget:
    return MaintenanceBudget.start(
        timeout_seconds=getattr(
            settings,
            "storage_maintenance_timeout_seconds",
            _MAINTENANCE_TIMEOUT_SECONDS,
        ),
        max_hash_bytes=getattr(
            settings,
            "storage_maintenance_max_hash_bytes",
            _MAINTENANCE_MAX_HASH_BYTES,
        ),
        max_directory_entries=getattr(
            settings,
            "storage_maintenance_max_directory_entries",
            _MAINTENANCE_MAX_DIRECTORY_ENTRIES,
        ),
    )


def _budget_error_text(exc: MaintenanceBudgetExceeded) -> str:
    return (
        "storage maintenance budget exhausted "
        f"(resource={exc.resource}, phase={exc.phase}, "
        f"used={exc.used}, limit={exc.limit})"
    )


def build_storage_maintenance_job(settings: Settings):
    """Return the scheduler-safe zero-argument lifecycle job."""

    def job() -> None:
        maintenance_budget = _storage_maintenance_budget(settings)
        staging_report = reconcile_staging_files(
            get_engine(),
            settings,
            maintenance_budget=maintenance_budget,
        )
        if (
            staging_report.unresolved
            or staging_report.truncated
            or staging_report.errors
        ):
            logger.warning(
                "storage staging reconciliation requires follow-up: %s",
                staging_report,
            )
        with session_scope() as session:
            run_storage_maintenance(
                session,
                settings,
                maintenance_budget=maintenance_budget,
            )
        try:
            with session_scope() as session:
                measure_usage(session, settings)
        except (OSError, TimeoutError):
            logger.warning(
                "storage usage measurement was deferred after maintenance",
                exc_info=True,
            )

    return job


def _now() -> datetime:
    return clock.utc_now()


def ensure_default_policies(session: Session) -> list[RetentionPolicy]:
    policies = {
        row.data_class: row
        for row in session.scalars(select(RetentionPolicy))
    }
    if DEFAULT_RETENTION.keys() <= policies.keys():
        return sorted(policies.values(), key=lambda row: row.data_class)

    lock_activity_write_plane(session)
    policies = {
        row.data_class: row
        for row in session.scalars(select(RetentionPolicy))
    }
    missing = [
        {
            "data_class": data_class,
            "retention_days": RETENTION_PRESETS[preset],
            "enabled": True,
        }
        for data_class, preset in DEFAULT_RETENTION.items()
        if data_class not in policies
    ]
    dialect = session.get_bind().dialect.name
    if missing and dialect in {"postgresql", "sqlite"}:
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        session.execute(
            insert(RetentionPolicy)
            .values(missing)
            .on_conflict_do_nothing(
                index_elements=[RetentionPolicy.data_class]
            )
        )
    elif missing:
        session.add_all(
            RetentionPolicy(**values)
            for values in missing
        )
    session.flush()
    return list(
        session.scalars(
            select(RetentionPolicy).order_by(
                RetentionPolicy.data_class
            )
        )
    )


def update_retention_policy(
    session: Session,
    data_class: str,
    preset: str,
    *,
    now: datetime | None = None,
) -> RetentionPolicy:
    with activity_write_lock():
        return _update_retention_policy(
            session,
            data_class,
            preset,
            now=now,
        )


def _update_retention_policy(
    session: Session,
    data_class: str,
    preset: str,
    *,
    now: datetime | None = None,
) -> RetentionPolicy:
    if preset not in RETENTION_PRESETS:
        raise ValueError(f"unsupported retention preset: {preset}")
    current = _as_utc(now or _now())
    # Input descriptor revisions include every retention class. All retention
    # writers must therefore share the same cross-process fence as input CAS.
    lock_activity_write_plane(session)
    if data_class in {"alert", "decision"}:
        lock_trigger_events_for_retention(session)
    ensure_default_policies(session)
    policy = session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.data_class == data_class)
    )
    if policy is None:
        policy = RetentionPolicy(data_class=data_class, enabled=True)
        session.add(policy)
    previous_retention_days = policy.retention_days
    policy.enabled = True
    policy.retention_days = RETENTION_PRESETS[preset]
    session.flush()
    _recalculate_expiry(
        session,
        policy,
        previous_retention_days=previous_retention_days,
        now=current,
    )
    if data_class == "calendar_mirror":
        purge_expired_calendar_mirrors(
            session,
            cutoff=retention_cutoff(
                session,
                "calendar_mirror",
                now=current,
            ),
        )
    if data_class == "decision":
        # Sessions disable autoflush. Persist recalculated expiry timestamps
        # before scrubbing receipts and deleting DecisionRecords.
        # ``purge_expired_decision_records`` owns the inseparable trigger-answer
        # scrub so no caller can bypass it.
        session.flush()
        scrub_decision_receipt_results(
            session,
            now=current,
        )
        purge_expired_decision_records(
            session,
            now=current,
        )
    if data_class == "alert":
        expire_trigger_event_answers(
            session,
            now=current,
        )
    if data_class.startswith("activity_"):
        session.flush()
        from healthmes.activity.maintenance import run_activity_maintenance

        run_activity_maintenance(session, now=current)
    return policy


def retention_cutoff(
    session: Session,
    data_class: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return the enforced UTC cutoff for a retained database data class."""
    policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == data_class
        )
    )
    if policy is None:
        preset = DEFAULT_RETENTION.get(data_class)
        retention_days = (
            RETENTION_PRESETS[preset]
            if preset is not None
            else None
        )
        enabled = preset is not None
    else:
        retention_days = policy.retention_days
        enabled = policy.enabled
    if not enabled or retention_days is None:
        return None
    return _as_utc(now or _now()) - timedelta(
        days=retention_days
    )


def _expiry(policy: RetentionPolicy, observed_at: datetime) -> datetime | None:
    if not policy.enabled or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def _recalculate_expiry(
    session: Session,
    policy: RetentionPolicy,
    *,
    previous_retention_days: int | None,
    now: datetime,
) -> None:
    current = _as_utc(now)
    for obj in session.scalars(
        select(StorageObject).where(
            StorageObject.data_class == policy.data_class,
            StorageObject.purged_at.is_(None),
        )
    ):
        if (
            obj.expires_at is not None
            and _as_utc(obj.expires_at) <= current
        ):
            continue
        obj.retention_policy_id = policy.id
        basis = obj.retention_basis_at
        if (
            basis is None
            and obj.expires_at is not None
            and previous_retention_days is not None
        ):
            basis = obj.expires_at - timedelta(
                days=previous_retention_days
            )
        basis = basis or obj.created_at
        obj.retention_basis_at = basis
        obj.expires_at = _expiry(policy, basis)
    for event in session.scalars(
        select(WellnessEvent).where(WellnessEvent.retention_policy_id == policy.id)
    ):
        if (
            event.expires_at is not None
            and _as_utc(event.expires_at) <= current
        ):
            continue
        event.expires_at = _expiry(policy, event.observed_at)
    if policy.data_class == "decision":
        for row in session.scalars(
            select(DecisionRecord).where(
                DecisionRecord.decision_request_id.is_not(None)
            )
        ):
            if (
                row.expires_at is not None
                and _as_utc(row.expires_at) <= current
            ):
                continue
            basis = _as_utc(
                row.retention_basis_at or row.created_at
            )
            row.retention_basis_at = basis
            row.expires_at = _expiry(policy, basis)
    if (
        policy.data_class == "activity_raw"
        and policy.enabled
    ):
        finite_windows = [
            days
            for days in (
                previous_retention_days,
                policy.retention_days,
            )
            if days is not None
        ]
        retention_days = min(finite_windows) if finite_windows else None
        if retention_days is not None:
            # Rows already hidden by the previous finite policy are physical
            # history, not dormant data that may reappear when switching to
            # forever. Permanently remove that expired compatibility tail.
            cutoff = current - timedelta(days=retention_days)
            session.execute(
                delete(AppUsageSample).where(
                    AppUsageSample.bucket_start <= cutoff
                )
            )


def apply_decision_retention(
    session: Session,
    row: DecisionRecord,
    *,
    basis_at: datetime,
) -> DecisionRecord:
    """Classify one Decision Agent record under the user-owned policy."""

    policies = {
        policy.data_class: policy
        for policy in ensure_default_policies(session)
    }
    policy = policies["decision"]
    basis = _as_utc(basis_at)
    row.retention_basis_at = basis
    row.expires_at = _expiry(policy, basis)
    return row


def purge_expired_decision_records(
    session: Session,
    *,
    now: datetime,
    dry_run: bool = False,
) -> int:
    """Scrub linked answers, then delete expired Wellness decisions.

    Trigger payloads duplicate the user-facing answer and correlation fields.
    Keeping the scrub inside this canonical purge prevents direct callers such
    as DecisionFinalizer from deleting the DecisionRecord first and stranding
    sensitive answer text in ``TriggerEvent.payload``.
    """

    current = _as_utc(now)
    expire_trigger_event_answers(
        session,
        now=current,
        dry_run=dry_run,
    )
    expired = (
        DecisionRecord.decision_request_id.is_not(None),
        DecisionRecord.retention_basis_at.is_not(None),
        DecisionRecord.expires_at.is_not(None),
        DecisionRecord.expires_at <= current,
    )
    candidates = int(
        session.scalar(
            select(func.count())
            .select_from(DecisionRecord)
            .where(*expired)
        )
        or 0
    )
    if candidates and not dry_run:
        session.execute(
            delete(DecisionRecord)
            .where(*expired)
            .execution_options(synchronize_session=False)
        )
    return candidates


def register_storage_object(
    session: Session,
    settings: Settings,
    *,
    relative_path: str,
    data_class: str,
    content_type: str | None,
    size_bytes: int,
    sha256: str | None = None,
    observed_at: datetime | None = None,
    safe_to_purge: bool = True,
) -> StorageObject:
    existing = session.scalar(
        select(StorageObject).where(StorageObject.relative_path == relative_path)
    )
    if existing is not None:
        if existing.purged_at is not None:
            raise ValueError(
                f"storage path was already purged and cannot be reused: "
                f"{relative_path}"
            )
        if (
            existing.content_type != content_type
            or existing.size_bytes != size_bytes
            or existing.sha256 != sha256
        ):
            raise ValueError(
                f"storage path already indexes a different payload: "
                f"{relative_path}"
            )
        return existing
    policies = {row.data_class: row for row in ensure_default_policies(session)}
    policy = policies.get(data_class)
    observed = observed_at or _now()
    obj = StorageObject(
        data_class=data_class,
        relative_path=relative_path,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        retention_policy_id=policy.id if policy else None,
        retention_basis_at=observed,
        expires_at=_expiry(policy, observed) if policy else None,
        safe_to_purge=safe_to_purge,
    )
    session.add(obj)
    session.flush()
    return obj


def classify_storage_object(
    session: Session,
    obj: StorageObject,
    *,
    data_class: str,
    observed_at: datetime,
    safe_to_purge: bool,
) -> StorageObject:
    """Move an indexed object under a purpose-specific retention policy."""
    policies = {row.data_class: row for row in ensure_default_policies(session)}
    policy = policies[data_class]
    already_classified = obj.data_class == data_class
    obj.data_class = data_class
    obj.retention_policy_id = policy.id
    if not already_classified or obj.retention_basis_at is None:
        obj.retention_basis_at = observed_at
        obj.expires_at = _expiry(policy, observed_at)
    obj.safe_to_purge = safe_to_purge
    session.flush()
    return obj


def index_raw_ingest(
    session: Session, settings: Settings, raw: RawIngestEvent
) -> WellnessEvent:
    obj = register_storage_object(
        session,
        settings,
        relative_path=raw.path,
        data_class="raw_payload",
        content_type=raw.content_type,
        size_bytes=raw.size_bytes,
        sha256=raw.sha256,
        observed_at=raw.received_at,
    )
    existing = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == raw.source,
            WellnessEvent.source_record_id == str(raw.id),
        )
    )
    if existing is not None:
        return existing
    policy = session.get(RetentionPolicy, obj.retention_policy_id)
    event = WellnessEvent(
        event_type="raw_ingest",
        observed_at=raw.received_at,
        recorded_at=raw.received_at,
        source_provider=raw.source,
        source_record_id=str(raw.id),
        capture_method="import",
        retention_policy_id=obj.retention_policy_id,
        expires_at=_expiry(policy, raw.received_at) if policy else None,
        payload={
            "content_type": raw.content_type,
            "size_bytes": raw.size_bytes,
            "parse_status": raw.parse_status,
            "forward_status": raw.forward_status,
        },
        raw_object_id=obj.id,
    )
    session.add(event)
    session.flush()
    return event


def _class_for(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    first = relative.parts[0] if relative.parts else "other"
    return {
        "raw_ingest": "raw_payload",
        "media": "media",
        "backups": "backup",
        "exports": "export",
    }.get(first, "other")


def _is_cleanup_quarantine_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    cleanup_journal_parts = Path(_CLEANUP_JOURNAL_DIRECTORY).parts
    if parts[: len(cleanup_journal_parts)] == cleanup_journal_parts:
        return True
    if (
        len(parts) == 2
        and parts[0] == ".staging"
        and (
            parts[1] in _INTERNAL_STORAGE_CONTROL_NAMES
            or _INTERNAL_STORAGE_CONTROL_TEMP_NAME.fullmatch(parts[1])
            is not None
        )
    ):
        return True
    for part in parts:
        if (
            part == _DURABLE_UNLINK_RECOVERY_DIRECTORY
            or _CLEANUP_QUARANTINE_NAME.fullmatch(part) is not None
            or _DURABLE_UNLINK_QUARANTINE_NAME.fullmatch(part) is not None
        ):
            return True
    return False


def _fresh_discovery_state() -> _DiscoveryState:
    # A deterministic restart from each root cannot both honor max_entries
    # and make progress. Persist one independent DFS stack per storage class.
    return _DiscoveryState(
        next_class=_DISCOVERY_ROOTS[0][0],
        stacks={
            root_name: [_DiscoveryFrame(component=None)]
            for root_name, _data_class in _DISCOVERY_ROOTS
        },
    )


def _valid_discovery_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and "/" not in value
        and (os.name != "nt" or "\\" not in value)
        and "\x00" not in value
        and len(value) <= 1024
    )


def _decode_discovery_state(payload: bytes) -> _DiscoveryState:
    value = json.loads(payload.decode("utf-8"))
    root_names = {root_name for root_name, _ in _DISCOVERY_ROOTS}
    if (
        not isinstance(value, dict)
        or value.get("version") != 2
        or value.get("next_class") not in root_names
        or not isinstance(value.get("classes"), dict)
    ):
        raise ValueError("unsupported unindexed discovery cursor")
    classes = value["classes"]
    if set(classes) != root_names:
        raise ValueError("unindexed discovery cursor classes do not match")
    stacks: dict[str, list[_DiscoveryFrame]] = {}
    for root_name, _data_class in _DISCOVERY_ROOTS:
        raw_stack = classes[root_name]
        if (
            not isinstance(raw_stack, list)
            or not raw_stack
            or len(raw_stack) > _DISCOVERY_MAX_DEPTH
        ):
            raise ValueError("invalid unindexed discovery cursor stack")
        stack: list[_DiscoveryFrame] = []
        for depth, raw_frame in enumerate(raw_stack):
            if not isinstance(raw_frame, dict):
                raise ValueError("invalid unindexed discovery cursor frame")
            component = raw_frame.get("component")
            if (
                (depth == 0 and component is not None)
                or (
                    depth > 0
                    and not _valid_discovery_component(component)
                )
            ):
                raise ValueError(
                    "invalid unindexed discovery cursor position"
                )
            identity_values = tuple(
                raw_frame.get(field)
                for field in (
                    "device",
                    "inode",
                    "mtime_ns",
                    "ctime_ns",
                )
            )
            if any(
                value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                )
                for value in identity_values
            ) or (
                any(value is None for value in identity_values)
                and not all(value is None for value in identity_values)
            ):
                raise ValueError(
                    "invalid unindexed discovery directory identity"
                )
            offset = raw_frame.get("offset", 0)
            batch_index = raw_frame.get("batch_index", 0)
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 0
                or offset > 2**63 - 1
                or isinstance(batch_index, bool)
                or not isinstance(batch_index, int)
                or batch_index < 0
                or batch_index > 4096
            ):
                raise ValueError(
                    "invalid unindexed discovery directory offset"
                )
            stack.append(
                _DiscoveryFrame(
                    component=component,
                    device=identity_values[0],
                    inode=identity_values[1],
                    mtime_ns=identity_values[2],
                    ctime_ns=identity_values[3],
                    offset=offset,
                    batch_index=batch_index,
                    rescan=raw_frame.get("rescan") is True,
                )
            )
        stacks[root_name] = stack
    return _DiscoveryState(
        next_class=value["next_class"],
        stacks=stacks,
    )


def _encode_discovery_state(state: _DiscoveryState) -> bytes:
    payload = json.dumps(
        {
            "classes": {
                root_name: [
                    {
                        "batch_index": frame.batch_index,
                        "component": frame.component,
                        "ctime_ns": frame.ctime_ns,
                        "device": frame.device,
                        "inode": frame.inode,
                        "mtime_ns": frame.mtime_ns,
                        "offset": frame.offset,
                        "rescan": frame.rescan,
                    }
                    for frame in state.stacks[root_name]
                ]
                for root_name, _data_class in _DISCOVERY_ROOTS
            },
            "next_class": state.next_class,
            "version": 2,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(payload) > _DISCOVERY_CURSOR_MAX_BYTES:
        raise ValueError("unindexed discovery cursor exceeds its size limit")
    return payload


@contextmanager
def _open_discovery_data_root(settings: Settings) -> Iterator[int]:
    root = settings.data_dir.expanduser()
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise _DiscoveryUnsafeAncestor(
                    f"storage root must be a directory: {root}"
                )
            yield descriptor
        finally:
            os.close(descriptor)
        return
    with open_directory_anchored(root) as (_canonical, descriptor):
        yield descriptor


@contextmanager
def _open_discovery_directory(
    root_descriptor: int,
    root: Path,
    parts: tuple[str, ...],
    *,
    create: bool = False,
) -> Iterator[int]:
    if not parts or any(
        not _valid_discovery_component(component) for component in parts
    ):
        raise ValueError("unsafe unindexed discovery path")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.dup(root_descriptor)
    descriptors = [current]
    current_path = root
    try:
        for component in parts:
            current_path /= component
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError as exc:
                if not create:
                    raise _DiscoveryAncestorMissing(
                        f"discovery directory is missing: {current_path}"
                    ) from exc
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                else:
                    os.fsync(current)
                try:
                    child = os.open(component, flags, dir_fd=current)
                except OSError as open_exc:
                    raise _DiscoveryUnsafeAncestor(
                        "discovery directory is unavailable or symlinked: "
                        f"{current_path}"
                    ) from open_exc
            except OSError as exc:
                raise _DiscoveryUnsafeAncestor(
                    "discovery directory is unavailable or symlinked: "
                    f"{current_path}"
                ) from exc
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise _DiscoveryUnsafeAncestor(
                    f"discovery path is not a directory: {current_path}"
                )
            current = child
            descriptors.append(current)
        yield current
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _secure_discovery_control_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if metadata.st_uid != current_uid:
        raise _DiscoveryUnsafeAncestor(
            "unindexed discovery control directory is not user-owned"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)


def _read_discovery_state(parent_descriptor: int) -> _DiscoveryState:
    try:
        descriptor = os.open(
            _DISCOVERY_CURSOR_NAME,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        return _fresh_discovery_state()
    except OSError as exc:
        logger.warning(
            "ignoring unsafe unindexed discovery cursor: %s",
            exc,
        )
        return _fresh_discovery_state()
    try:
        metadata = os.fstat(descriptor)
        current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != current_uid
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _DISCOVERY_CURSOR_MAX_BYTES
        ):
            raise ValueError(
                "unindexed discovery cursor must be a small owner-only "
                "regular file"
            )
        payload = bytearray()
        while len(payload) <= _DISCOVERY_CURSOR_MAX_BYTES:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _DISCOVERY_CURSOR_MAX_BYTES:
            raise ValueError(
                "unindexed discovery cursor exceeds its size limit"
            )
    except (OSError, ValueError) as exc:
        logger.warning("ignoring invalid unindexed discovery cursor: %s", exc)
        return _fresh_discovery_state()
    finally:
        os.close(descriptor)
    try:
        return _decode_discovery_state(bytes(payload))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("ignoring malformed unindexed discovery cursor: %s", exc)
        return _fresh_discovery_state()


def _write_discovery_state(
    parent_descriptor: int,
    state: _DiscoveryState,
) -> None:
    payload = _encode_discovery_state(state)
    temporary = f"{_DISCOVERY_CURSOR_NAME}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(
                    "unindexed discovery cursor write made no progress"
                )
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        os.replace(
            temporary,
            _DISCOVERY_CURSOR_NAME,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise


def _discovery_stack_parts(
    root_name: str,
    stack: list[_DiscoveryFrame],
) -> tuple[str, ...]:
    return (
        root_name,
        *(
            frame.component
            for frame in stack[1:]
            if frame.component is not None
        ),
    )


def _discovery_generation(
    metadata: os.stat_result,
) -> tuple[int, int, int, int]:
    if not stat.S_ISDIR(metadata.st_mode):
        raise _DiscoveryUnsafeAncestor(
            "unindexed discovery path is not a directory"
        )
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _frame_generation(
    frame: _DiscoveryFrame,
) -> tuple[int, int, int, int] | None:
    values = (
        frame.device,
        frame.inode,
        frame.mtime_ns,
        frame.ctime_ns,
    )
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def _set_frame_generation(
    frame: _DiscoveryFrame,
    generation: tuple[int, int, int, int],
) -> None:
    (
        frame.device,
        frame.inode,
        frame.mtime_ns,
        frame.ctime_ns,
    ) = generation


def _reset_discovery_frame(
    frame: _DiscoveryFrame,
    generation: tuple[int, int, int, int],
) -> None:
    _set_frame_generation(frame, generation)
    frame.offset = 0
    frame.batch_index = 0
    frame.rescan = False


def _index_discovered_entry(
    session: Session,
    settings: Settings,
    *,
    directory_descriptor: int,
    entry_name: str,
    relative_parts: tuple[str, ...],
    data_class: str,
    metadata: os.stat_result,
    known: set[str] | None,
    maintenance_budget: MaintenanceBudget | None = None,
) -> None:
    relative = "/".join(relative_parts)
    indexed = (
        relative in known
        if known is not None
        else session.scalar(
            select(StorageObject.id)
            .where(StorageObject.relative_path == relative)
            .limit(1)
        )
        is not None
    )
    if indexed:
        return
    if maintenance_budget is not None:
        maintenance_budget.reserve_hash_bytes(
            metadata.st_size,
            phase="unindexed payload discovery hash",
        )
    descriptor = os.open(
        entry_name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        expected_generation = _metadata_generation(metadata)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _metadata_generation(opened) != expected_generation
        ):
            raise OSError(
                "legacy payload changed before descriptor-bound hashing"
        )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            if maintenance_budget is not None:
                maintenance_budget.checkpoint(
                    phase="unindexed payload discovery hash"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = os.stat(
            entry_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            _metadata_generation(after) != expected_generation
            or _metadata_generation(current) != expected_generation
        ):
            raise OSError(
                "legacy payload changed during descriptor-bound hashing"
            )
    finally:
        os.close(descriptor)
    register_storage_object(
        session,
        settings,
        relative_path=relative,
        data_class=data_class,
        content_type=None,
        size_bytes=metadata.st_size,
        sha256=digest.hexdigest(),
        observed_at=datetime.fromtimestamp(metadata.st_mtime, UTC),
    )
    if known is not None:
        known.add(relative)


def _advance_discovery_class(
    session: Session,
    settings: Settings,
    *,
    root_descriptor: int,
    root_name: str,
    data_class: str,
    stack: list[_DiscoveryFrame],
    entry_budget: int,
    deadline: float | None,
    known: set[str] | None,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[int, bool]:
    inspected = 0
    while stack and inspected < entry_budget:
        if deadline is not None and time.monotonic() >= deadline:
            break
        parts = _discovery_stack_parts(root_name, stack)
        try:
            directory_context = _open_discovery_directory(
                root_descriptor,
                settings.data_dir.expanduser(),
                parts,
            )
            directory = directory_context.__enter__()
        except _DiscoveryAncestorMissing as exc:
            if len(stack) > 1:
                logger.warning("skipping stale discovery directory: %s", exc)
            stack.pop()
            continue
        except _DiscoveryUnsafeAncestor as exc:
            logger.warning("skipping unsafe discovery directory: %s", exc)
            stack.pop()
            continue
        try:
            frame = stack[-1]
            initial_generation = _discovery_generation(
                os.fstat(directory)
            )
            recorded_generation = _frame_generation(frame)
            if (
                recorded_generation is None
                or recorded_generation[:2] != initial_generation[:2]
            ):
                _reset_discovery_frame(frame, initial_generation)
            elif recorded_generation != initial_generation:
                frame.rescan = True
                _set_frame_generation(frame, initial_generation)
            try:
                entries, next_offset, complete = read_directory_batch(
                    directory,
                    frame.offset,
                )
            except (OSError, ValueError) as exc:
                if frame.offset != 0 or frame.batch_index != 0:
                    _reset_discovery_frame(frame, initial_generation)
                    continue
                logger.warning(
                    "skipping unreadable discovery directory %s: %s",
                    settings.data_dir.joinpath(*parts),
                    exc,
                )
                stack.pop()
                continue
            if frame.batch_index > len(entries):
                _reset_discovery_frame(frame, initial_generation)
                continue
            descended = False
            while (
                frame.batch_index < len(entries)
                and inspected < entry_budget
                and (
                    deadline is None
                    or time.monotonic() < deadline
                    or inspected == 0
                )
            ):
                entry_name = entries[frame.batch_index]
                if maintenance_budget is not None:
                    maintenance_budget.consume_directory_entry(
                        phase="unindexed payload discovery scan",
                        operation="scan",
                    )
                frame.batch_index += 1
                inspected += 1
                relative_parts = (*parts, entry_name)
                if not _valid_discovery_component(entry_name):
                    continue
                relative_path = Path(*relative_parts)
                if _is_cleanup_quarantine_path(relative_path):
                    continue
                try:
                    metadata = os.stat(
                        entry_name,
                        dir_fd=directory,
                        follow_symlinks=False,
                    )
                    if stat.S_ISDIR(metadata.st_mode):
                        if len(stack) >= _DISCOVERY_MAX_DEPTH:
                            logger.warning(
                                "unindexed discovery depth limit reached at %s",
                                settings.data_dir.joinpath(*relative_parts),
                            )
                            continue
                        stack.append(
                            _DiscoveryFrame(
                                component=entry_name,
                                device=metadata.st_dev,
                                inode=metadata.st_ino,
                                mtime_ns=metadata.st_mtime_ns,
                                ctime_ns=metadata.st_ctime_ns,
                            )
                        )
                        descended = True
                        break
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning(
                        "could not inspect legacy payload %s: %s",
                        settings.data_dir.joinpath(*relative_parts),
                        exc,
                    )
                    continue
                try:
                    _index_discovered_entry(
                        session,
                        settings,
                        directory_descriptor=directory,
                        entry_name=entry_name,
                        relative_parts=relative_parts,
                        data_class=data_class,
                        metadata=metadata,
                        known=known,
                        maintenance_budget=maintenance_budget,
                    )
                except MaintenanceBudgetExceeded:
                    # The cursor must continue to point at the unindexed file.
                    frame.batch_index -= 1
                    raise
                except (FileNotFoundError, OSError) as exc:
                    logger.warning(
                        "could not hash legacy payload %s: %s",
                        settings.data_dir.joinpath(*relative_parts),
                        exc,
                    )
            if descended:
                continue
            if frame.batch_index == len(entries):
                frame.offset = next_offset
                frame.batch_index = 0
            if complete and not entries:
                final_generation = _discovery_generation(
                    os.fstat(directory)
                )
                if final_generation != initial_generation:
                    frame.rescan = True
                _set_frame_generation(frame, final_generation)
                if frame.rescan:
                    _reset_discovery_frame(frame, final_generation)
                else:
                    stack.pop()
        finally:
            directory_context.__exit__(None, None, None)
    return inspected, not stack


def _discover_unindexed(
    session: Session,
    settings: Settings,
    *,
    max_entries: int | None = None,
    deadline: float | None = None,
    maintenance_budget: MaintenanceBudget | None = None,
) -> bool:
    """Index legacy payloads, optionally within a bounded maintenance slice.

    Returns whether more filesystem entries may remain uninspected.
    """
    root = settings.data_dir.expanduser()
    if not root.exists():
        return False
    if maintenance_budget is not None:
        maintenance_budget.checkpoint(
            phase="unindexed payload discovery"
        )
    bounded = max_entries is not None or deadline is not None
    known = (
        set(session.scalars(select(StorageObject.relative_path)))
        if not bounded
        else None
    )
    completed: set[str] = set()
    remaining = max_entries
    with _open_discovery_data_root(settings) as root_descriptor:
        if bounded:
            with _open_discovery_directory(
                root_descriptor,
                root,
                (".staging",),
                create=True,
            ) as control_descriptor:
                _secure_discovery_control_directory(control_descriptor)
                state = _read_discovery_state(control_descriptor)
                try:
                    while len(completed) < len(_DISCOVERY_ROOTS):
                        if (
                            remaining is not None
                            and remaining <= 0
                        ) or (
                            deadline is not None
                            and time.monotonic() >= deadline
                        ):
                            break
                        root_names = [
                            root_name
                            for root_name, _ in _DISCOVERY_ROOTS
                        ]
                        selected: tuple[str, str] | None = None
                        for _ in _DISCOVERY_ROOTS:
                            index = root_names.index(state.next_class)
                            root_name, data_class = _DISCOVERY_ROOTS[index]
                            state.next_class = _DISCOVERY_ROOTS[
                                (index + 1) % len(_DISCOVERY_ROOTS)
                            ][0]
                            if root_name not in completed:
                                selected = root_name, data_class
                                break
                        if selected is None:
                            break
                        root_name, data_class = selected
                        class_entry_budget = _DISCOVERY_CLASS_QUANTUM
                        if remaining is not None:
                            class_entry_budget = min(
                                class_entry_budget,
                                remaining,
                            )
                        inspected, class_complete = (
                            _advance_discovery_class(
                                session,
                                settings,
                                root_descriptor=root_descriptor,
                                root_name=root_name,
                                data_class=data_class,
                                stack=state.stacks[root_name],
                                entry_budget=class_entry_budget,
                                deadline=deadline,
                                known=known,
                                maintenance_budget=maintenance_budget,
                            )
                        )
                        if remaining is not None:
                            remaining -= inspected
                        if class_complete:
                            completed.add(root_name)
                        if inspected == 0 and not class_complete:
                            break
                finally:
                    for root_name in completed:
                        state.stacks[root_name] = [
                            _DiscoveryFrame(component=None)
                        ]
                    # Cursor durability is a fail-closed completion capsule:
                    # persist it even when the cooperative budget just ended.
                    _write_discovery_state(control_descriptor, state)
        else:
            state = _fresh_discovery_state()
            while len(completed) < len(_DISCOVERY_ROOTS):
                root_names = [
                    root_name for root_name, _ in _DISCOVERY_ROOTS
                ]
                index = root_names.index(state.next_class)
                root_name, data_class = _DISCOVERY_ROOTS[index]
                state.next_class = _DISCOVERY_ROOTS[
                    (index + 1) % len(_DISCOVERY_ROOTS)
                ][0]
                if root_name in completed:
                    continue
                _inspected, class_complete = _advance_discovery_class(
                    session,
                    settings,
                    root_descriptor=root_descriptor,
                    root_name=root_name,
                    data_class=data_class,
                    stack=state.stacks[root_name],
                    entry_budget=_DISCOVERY_CLASS_QUANTUM,
                    deadline=None,
                    known=known,
                    maintenance_budget=maintenance_budget,
                )
                if class_complete:
                    completed.add(root_name)
    return len(completed) < len(_DISCOVERY_ROOTS)


def measure_usage(
    session: Session,
    settings: Settings,
) -> dict[str, dict[str, int]]:
    if (
        os.name == "nt"
        or not getattr(os, "O_DIRECTORY", 0)
        or not getattr(os, "O_NOFOLLOW", 0)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
    ):
        raise OSError(
            "secure storage usage measurement requires descriptor-relative "
            "no-follow directory traversal"
        )
    with open_directory_anchored(settings.data_dir.expanduser()) as (
        root,
        anchored_root_descriptor,
    ):
        root_descriptor = os.dup(anchored_root_descriptor)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | os.O_NOFOLLOW
    )

    @dataclass(slots=True)
    class DirectoryFrame:
        descriptor: int
        iterator: Iterator[os.DirEntry[str]]
        relative_parts: tuple[str, ...]
        generation: tuple[int, ...]
        parent_descriptor: int | None = None
        entry_name: str | None = None

        def close(self) -> None:
            close_iterator = getattr(self.iterator, "close", None)
            if callable(close_iterator):
                close_iterator()
            os.close(self.descriptor)

        def assert_stable(self) -> None:
            if _metadata_generation(os.fstat(self.descriptor)) != self.generation:
                raise OSError(
                    "storage usage directory changed during measurement"
                )
            if self.parent_descriptor is None or self.entry_name is None:
                return
            current = os.stat(
                self.entry_name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
            if _metadata_generation(current) != self.generation:
                raise OSError(
                    "storage usage directory generation changed during "
                    "measurement"
                )

    def open_frame(
        descriptor: int,
        *,
        relative_parts: tuple[str, ...],
        expected: os.stat_result | None = None,
        parent_descriptor: int | None = None,
        entry_name: str | None = None,
    ) -> DirectoryFrame:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("storage usage path is not a directory")
        if (
            expected is not None
            and _metadata_generation(metadata)
            != _metadata_generation(expected)
        ):
            raise OSError(
                "storage usage directory changed before it was opened"
            )
        iterator = os.scandir(descriptor)
        return DirectoryFrame(
            descriptor=descriptor,
            iterator=iterator,
            relative_parts=relative_parts,
            generation=_metadata_generation(metadata),
            parent_descriptor=parent_descriptor,
            entry_name=entry_name,
        )

    totals: dict[str, dict[str, int]] = {}
    indexed = {
        row.relative_path: row.data_class
        for row in session.scalars(
            select(StorageObject).where(StorageObject.purged_at.is_(None))
        )
    }
    physical_files: dict[
        tuple[int, int],
        tuple[tuple[int, int, str], str, int],
    ] = {}
    inspected = 0
    deadline = time.monotonic() + _USAGE_SCAN_MAX_SECONDS
    try:
        root_frame = open_frame(root_descriptor, relative_parts=())
    except BaseException:
        os.close(root_descriptor)
        raise
    directories = [root_frame]
    try:
        while directories:
            if (
                inspected >= _USAGE_SCAN_ENTRY_LIMIT
                or time.monotonic() >= deadline
            ):
                raise TimeoutError(
                    "storage usage scan exceeded its bounded slice"
                )
            try:
                frame = directories[-1]
                entry = next(frame.iterator)
            except StopIteration:
                frame.assert_stable()
                directories.pop().close()
                continue
            except OSError as exc:
                raise OSError(
                    "could not complete storage usage directory scan"
                ) from exc
            inspected += 1
            entry_name = entry.name
            relative_parts = (*frame.relative_parts, entry_name)
            relative_path = Path(*relative_parts)
            try:
                metadata = os.stat(
                    entry_name,
                    dir_fd=frame.descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise OSError(
                    "could not inspect a storage usage entry"
                ) from exc
            if stat.S_ISDIR(metadata.st_mode):
                if len(directories) >= _DISCOVERY_MAX_DEPTH:
                    raise OSError(
                        "storage usage directory depth exceeds the safety limit"
                    )
                child_descriptor: int | None = None
                try:
                    child_descriptor = os.open(
                        entry_name,
                        directory_flags,
                        dir_fd=frame.descriptor,
                    )
                    child = open_frame(
                        child_descriptor,
                        relative_parts=relative_parts,
                        expected=metadata,
                        parent_descriptor=frame.descriptor,
                        entry_name=entry_name,
                    )
                    child_descriptor = None
                    current = os.stat(
                        entry_name,
                        dir_fd=frame.descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        _metadata_generation(current)
                        != child.generation
                    ):
                        child.close()
                        raise OSError(
                            "storage usage directory changed while it was "
                            "opened"
                        )
                    directories.append(child)
                except OSError as exc:
                    if child_descriptor is not None:
                        os.close(child_descriptor)
                    raise OSError(
                        "could not safely open a storage usage directory"
                    ) from exc
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if _is_cleanup_quarantine_path(relative_path):
                continue
            relative = relative_path.as_posix()
            indexed_class = indexed.get(relative)
            data_class = indexed_class or {
                "raw_ingest": "raw_payload",
                "media": "media",
                "backups": "backup",
                "exports": "export",
            }.get(relative_parts[0], "other")
            priority = (
                0 if indexed_class is not None else 1,
                1
                if relative_path.parts
                and relative_path.parts[0] == ".staging"
                else 0,
                relative,
            )
            identity = metadata.st_dev, metadata.st_ino
            previous = physical_files.get(identity)
            if previous is None or priority < previous[0]:
                physical_files[identity] = (
                    priority,
                    data_class,
                    metadata.st_size,
                )
    finally:
        for directory in reversed(directories):
            directory.close()

    for _priority, data_class, size_bytes in physical_files.values():
        bucket = totals.setdefault(data_class, {"bytes": 0, "objects": 0})
        bucket["bytes"] += size_bytes
        bucket["objects"] += 1

    today = date.today()
    existing = {
        row.data_class: row
        for row in session.scalars(
            select(StorageUsageDaily).where(
                StorageUsageDaily.measured_on == today,
                StorageUsageDaily.provider == "local",
            )
        )
    }
    known_classes = set(
        session.scalars(
            select(StorageUsageDaily.data_class).where(
                StorageUsageDaily.provider == "local"
            )
        )
    )
    for data_class in existing.keys() | known_classes | totals.keys():
        values = totals.get(data_class, {"bytes": 0, "objects": 0})
        row = existing.get(data_class)
        if row is None:
            row = StorageUsageDaily(
                measured_on=today, provider="local", data_class=data_class
            )
            session.add(row)
        row.bytes_used = values["bytes"]
        row.object_count = values["objects"]
    session.flush()
    return totals


def load_latest_usage_snapshot(
    session: Session,
    *,
    provider: str = "local",
) -> StorageUsageSnapshot:
    measured_on = session.scalar(
        select(func.max(StorageUsageDaily.measured_on)).where(
            StorageUsageDaily.provider == provider
        )
    )
    if measured_on is None:
        return StorageUsageSnapshot(
            provider=provider,
            measured_on=None,
            usage={},
        )
    rows = session.scalars(
        select(StorageUsageDaily)
        .where(
            StorageUsageDaily.provider == provider,
            StorageUsageDaily.measured_on == measured_on,
        )
        .order_by(StorageUsageDaily.data_class)
    )
    return StorageUsageSnapshot(
        provider=provider,
        measured_on=measured_on,
        usage={
            row.data_class: {
                "bytes": row.bytes_used,
                "objects": row.object_count,
            }
            for row in rows
            if row.bytes_used or row.object_count
        },
    )


def _validate_cleanup_relative_path(relative_path: str) -> tuple[str, ...]:
    if (
        not relative_path
        or "\x00" in relative_path
        or (os.name == "nt" and "\\" in relative_path)
        or relative_path.startswith("/")
    ):
        raise ValueError(f"unsafe path rejected: {relative_path}")
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe path rejected: {relative_path}")
    return parts


@contextmanager
def _open_cleanup_parent(
    settings: Settings,
    relative_path: str,
) -> Iterator[_CleanupParent]:
    parts = _validate_cleanup_relative_path(relative_path)
    data_root = settings.data_dir.expanduser()
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        try:
            root = data_root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise _CleanupAncestorUnavailable(
                f"cleanup storage root is unavailable: {data_root}"
            ) from exc
        parent = root
        for component in parts[:-1]:
            parent = parent / component
            try:
                metadata = parent.lstat()
            except FileNotFoundError as exc:
                raise _CleanupAncestorUnavailable(
                    f"cleanup ancestor is unavailable: {parent}"
                ) from exc
            if parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise OSError(
                    f"cleanup parent must be a real directory: {parent}"
                )
        yield _CleanupParent(path=parent, name=parts[-1], descriptor=None)
        return

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_context = open_directory_anchored(data_root)
    try:
        root, root_descriptor = root_context.__enter__()
    except FileNotFoundError as exc:
        raise _CleanupAncestorUnavailable(
            f"cleanup storage root is unavailable: {data_root}"
        ) from exc
    descriptors: list[int] = []
    current = root_descriptor
    try:
        for component in parts[:-1]:
            try:
                current = os.open(component, flags, dir_fd=current)
            except FileNotFoundError as exc:
                ancestor = root.joinpath(
                    *parts[: len(descriptors) + 1]
                )
                raise _CleanupAncestorUnavailable(
                    f"cleanup ancestor is unavailable: {ancestor}"
                ) from exc
            descriptors.append(current)
        yield _CleanupParent(
            path=root.joinpath(*parts[:-1]),
            name=parts[-1],
            descriptor=current,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        root_context.__exit__(None, None, None)


def _cleanup_entry_lstat(parent: _CleanupParent) -> os.stat_result:
    if parent.descriptor is None:  # pragma: no cover - Windows
        return (parent.path / parent.name).lstat()
    return os.stat(
        parent.name,
        dir_fd=parent.descriptor,
        follow_symlinks=False,
    )


def _cleanup_entry_readlink(parent: _CleanupParent) -> str:
    if parent.descriptor is None:  # pragma: no cover - Windows
        return os.readlink(parent.path / parent.name)
    return os.readlink(parent.name, dir_fd=parent.descriptor)


def _cleanup_entry_open(parent: _CleanupParent) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if parent.descriptor is None:  # pragma: no cover - Windows
        return os.open(parent.path / parent.name, flags)
    return os.open(parent.name, flags, dir_fd=parent.descriptor)


def _unlink_cleanup_entry(parent: _CleanupParent) -> None:
    if parent.descriptor is None:  # pragma: no cover - Windows
        (parent.path / parent.name).unlink()
        return
    os.unlink(parent.name, dir_fd=parent.descriptor)


def _fsync_cleanup_parent(parent: _CleanupParent) -> None:
    if parent.descriptor is None:  # pragma: no cover - Windows
        require_directory_entry_durability()
    os.fsync(parent.descriptor)


def _cleanup_journal_entry(
    parent: _CleanupParent,
    object_id: uuid.UUID,
    state: str,
) -> _CleanupParent:
    return _CleanupParent(
        path=parent.path,
        name=(
            f"{_CLEANUP_JOURNAL_PREFIX}{object_id.hex}-{state}.json"
        ),
        descriptor=parent.descriptor,
    )


def _cleanup_journal_relative_path(
    object_id: uuid.UUID,
    state: str,
) -> str:
    return (
        f"{_CLEANUP_JOURNAL_DIRECTORY}/"
        f"{_CLEANUP_JOURNAL_PREFIX}{object_id.hex}-{state}.json"
    )


def _secure_cleanup_journal_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("storage cleanup journal parent must be a directory")
    current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if metadata.st_uid != current_uid:
        raise OSError(
            "storage cleanup journal parent is not owned by the current user"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)


def _ensure_cleanup_journal_directory(
    settings: Settings,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> None:
    relative = _validate_cleanup_relative_path(
        f"{_CLEANUP_JOURNAL_DIRECTORY}/placeholder"
    )[:-1]
    data_root = settings.data_dir.expanduser()
    if os.name == "nt":  # pragma: no cover - Windows fails closed earlier
        require_directory_entry_durability()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with open_directory_anchored(data_root) as (
        _root,
        root_descriptor,
    ):
        descriptors: list[int] = []
        current = root_descriptor
        _secure_cleanup_journal_directory(current)
        try:
            for component in relative:
                try:
                    child = os.open(component, flags, dir_fd=current)
                except FileNotFoundError:
                    if maintenance_budget is not None:
                        maintenance_budget.consume_directory_entry(
                            phase=(
                                "storage cleanup journal directory creation"
                            ),
                            operation="mkdir",
                        )
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        # Another cooperative creator won the race after the
                        # reservation. The conservative charge remains spent.
                        pass
                    else:
                        os.fsync(current)
                    child = os.open(component, flags, dir_fd=current)
                current = child
                descriptors.append(current)
                _secure_cleanup_journal_directory(current)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


@contextmanager
def _open_cleanup_journal_parent(
    settings: Settings,
    object_id: uuid.UUID,
    *,
    create: bool = False,
    maintenance_budget: MaintenanceBudget | None = None,
) -> Iterator[_CleanupParent]:
    if create:
        _ensure_cleanup_journal_directory(
            settings,
            maintenance_budget=maintenance_budget,
        )
    try:
        with _open_cleanup_parent(
            settings,
            _cleanup_journal_relative_path(object_id, "intent"),
        ) as parent:
            yield parent
    except _CleanupAncestorUnavailable as exc:
        if not settings.data_dir.exists():
            raise
        raise FileNotFoundError(
            "storage cleanup journal directory does not exist"
        ) from exc


def _cleanup_journal_encode(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _cleanup_journal_read(entry: _CleanupParent) -> bytes | None:
    try:
        descriptor = _cleanup_entry_open(entry)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _CLEANUP_JOURNAL_MAX_BYTES
        ):
            raise ValueError(
                f"invalid storage cleanup journal: {entry.path / entry.name}"
            )
        payload = bytearray()
        while len(payload) <= _CLEANUP_JOURNAL_MAX_BYTES:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _CLEANUP_JOURNAL_MAX_BYTES:
            raise ValueError(
                f"oversized storage cleanup journal: {entry.path / entry.name}"
            )
        after = os.fstat(descriptor)
        if _metadata_generation(after) != _metadata_generation(metadata):
            raise OSError(
                f"storage cleanup journal changed while reading: "
                f"{entry.path / entry.name}"
            )
        return bytes(payload)
    finally:
        os.close(descriptor)


def _cleanup_journal_write(
    entry: _CleanupParent,
    payload: bytes,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
    replace_existing: bool = False,
    phase: str = "storage cleanup journal publication",
) -> None:
    if len(payload) > _CLEANUP_JOURNAL_MAX_BYTES:
        raise ValueError("storage cleanup journal payload is too large")
    if maintenance_budget is not None and mutations_precharged:
        raise ValueError(
            "storage cleanup journal mutations cannot be both budgeted "
            "and precharged"
        )
    if maintenance_budget is not None:
        maintenance_budget.reserve_directory_entries(
            3,
            phase=phase,
            operation="mutation",
        )
    temporary_name = f"{entry.name}.tmp-{uuid.uuid4().hex}"
    temporary_path = entry.path / temporary_name
    descriptor: int | None = None
    published = False
    try:
        if entry.descriptor is None:  # pragma: no cover - Windows
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        else:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=entry.descriptor,
            )
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("storage cleanup journal write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if entry.descriptor is None:  # pragma: no cover - Windows
            if replace_existing:
                os.replace(temporary_path, entry.path / entry.name)
            else:
                os.rename(temporary_path, entry.path / entry.name)
        elif replace_existing:
            os.replace(
                temporary_name,
                entry.name,
                src_dir_fd=entry.descriptor,
                dst_dir_fd=entry.descriptor,
            )
        else:
            os.link(
                temporary_name,
                entry.name,
                src_dir_fd=entry.descriptor,
                dst_dir_fd=entry.descriptor,
                follow_symlinks=False,
            )
        _fsync_cleanup_parent(entry)
        published = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if entry.descriptor is None:  # pragma: no cover - Windows
                temporary_path.unlink(missing_ok=True)
            else:
                os.unlink(temporary_name, dir_fd=entry.descriptor)
                _fsync_cleanup_parent(entry)
        except FileNotFoundError:
            pass
        except OSError:
            if not published:
                raise
            logger.warning(
                "completed storage cleanup journal left a recoverable "
                "temporary entry: %s",
                temporary_path,
            )


def _cleanup_journal_unlink(
    entry: _CleanupParent,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> None:
    try:
        metadata = _cleanup_entry_lstat(entry)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(
            f"storage cleanup journal is not a regular file: "
            f"{entry.path / entry.name}"
        )
    expected = DurableFileIdentity.from_metadata(metadata)
    durable_unlink(
        entry.path / entry.name,
        missing_ok=True,
        expected=expected,
        budget=maintenance_budget,
    )


def _cleanup_journal_decode(
    payload: bytes,
    *,
    path: Path,
) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid storage cleanup journal: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid storage cleanup journal: {path}")
    if _cleanup_journal_encode(value) != payload:
        raise ValueError(f"non-canonical storage cleanup journal: {path}")
    return value


def _cleanup_journal_intent_payload(
    candidate: _PendingFileCleanup,
    guarded_generations: frozenset[tuple[int, int]],
) -> dict[str, object]:
    identity = _cleanup_identity_without_manual_review(
        _normalize_cleanup_identity(candidate.identity)
    )
    return {
        "version": 1,
        "object_id": str(candidate.object_id),
        "relative_path": candidate.relative_path,
        "identity": identity,
        "guarded_generations": [
            {"device": device, "inode": inode}
            for device, inode in sorted(guarded_generations)
        ],
    }


def _parse_cleanup_journal_generations(
    value: object,
    *,
    path: Path,
    allow_empty: bool = False,
) -> frozenset[tuple[int, int]]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
    ):
        raise ValueError(f"invalid storage cleanup journal generations: {path}")
    generations: set[tuple[int, int]] = set()
    for generation in value:
        if (
            not isinstance(generation, dict)
            or set(generation) != {"device", "inode"}
        ):
            raise ValueError(
                f"invalid storage cleanup journal generation: {path}"
            )
        device = generation.get("device")
        inode = generation.get("inode")
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or device < 0
            or isinstance(inode, bool)
            or not isinstance(inode, int)
            or inode < 0
        ):
            raise ValueError(
                f"invalid storage cleanup journal generation: {path}"
            )
        generations.add((device, inode))
    if len(generations) != len(value):
        raise ValueError(
            f"duplicate storage cleanup journal generation: {path}"
        )
    return frozenset(generations)


def _parse_cleanup_journal_generation(
    value: object,
    *,
    path: Path,
) -> tuple[int, int]:
    generations = _parse_cleanup_journal_generations(
        [value],
        path=path,
    )
    return next(iter(generations))


def _read_cleanup_journal(
    settings: Settings,
    candidate: _PendingFileCleanup,
) -> _CleanupJournalState | None:
    try:
        parent_context = _open_cleanup_journal_parent(
            settings,
            candidate.object_id,
        )
        parent = parent_context.__enter__()
    except FileNotFoundError:
        return None
    try:
        intent_entry = _cleanup_journal_entry(
            parent,
            candidate.object_id,
            "intent",
        )
        complete_entry = _cleanup_journal_entry(
            parent,
            candidate.object_id,
            "complete",
        )
        progress_entry = _cleanup_journal_entry(
            parent,
            candidate.object_id,
            "progress",
        )
        manual_entry = _cleanup_journal_entry(
            parent,
            candidate.object_id,
            "manual-review",
        )
        intent_bytes = _cleanup_journal_read(intent_entry)
        progress_bytes = _cleanup_journal_read(progress_entry)
        complete_bytes = _cleanup_journal_read(complete_entry)
        manual_bytes = _cleanup_journal_read(manual_entry)
        if intent_bytes is None:
            if (
                progress_bytes is not None
                or complete_bytes is not None
                or manual_bytes is not None
            ):
                raise ValueError(
                    "storage cleanup journal state exists without its intent"
                )
            return None

        intent = _cleanup_journal_decode(
            intent_bytes,
            path=intent_entry.path / intent_entry.name,
        )
        if (
            intent.get("version") != 1
            or intent.get("object_id") != str(candidate.object_id)
            or intent.get("relative_path") != candidate.relative_path
            or _cleanup_identity_without_manual_review(
                _normalize_cleanup_identity(intent.get("identity"))
            )
            != _cleanup_identity_without_manual_review(
                _normalize_cleanup_identity(candidate.identity)
            )
        ):
            raise ValueError(
                "storage cleanup journal does not match the pending object"
            )
        guarded_generations = _parse_cleanup_journal_generations(
            intent.get("guarded_generations"),
            path=intent_entry.path / intent_entry.name,
        )
        intent_sha256 = hashlib.sha256(intent_bytes).hexdigest()

        completed_generations: frozenset[tuple[int, int]] = frozenset()
        removed_generations: frozenset[tuple[int, int]] = frozenset()
        active_generation: tuple[int, int] | None = None
        if progress_bytes is not None:
            progress_payload = _cleanup_journal_decode(
                progress_bytes,
                path=progress_entry.path / progress_entry.name,
            )
            if set(progress_payload) != {
                "version",
                "intent_sha256",
                "completed_generations",
                "removed_generations",
                "active_generation",
            } or (
                progress_payload.get("version") != 1
                or progress_payload.get("intent_sha256") != intent_sha256
            ):
                raise ValueError(
                    "invalid storage cleanup progress journal"
                )
            completed_generations = _parse_cleanup_journal_generations(
                progress_payload.get("completed_generations"),
                path=progress_entry.path / progress_entry.name,
                allow_empty=True,
            )
            removed_generations = _parse_cleanup_journal_generations(
                progress_payload.get("removed_generations"),
                path=progress_entry.path / progress_entry.name,
                allow_empty=True,
            )
            raw_active = progress_payload.get("active_generation")
            if raw_active is not None:
                active_generation = _parse_cleanup_journal_generation(
                    raw_active,
                    path=progress_entry.path / progress_entry.name,
                )
            if (
                not completed_generations.issubset(guarded_generations)
                or not removed_generations.issubset(completed_generations)
                or active_generation in completed_generations
                or (
                    active_generation is not None
                    and active_generation not in guarded_generations
                )
            ):
                raise ValueError(
                    "storage cleanup progress journal does not match intent"
                )

        complete = False
        if complete_bytes is not None:
            complete_payload = _cleanup_journal_decode(
                complete_bytes,
                path=complete_entry.path / complete_entry.name,
            )
            if complete_payload != {
                "version": 1,
                "intent_sha256": intent_sha256,
            }:
                raise ValueError(
                    "storage cleanup completion journal does not match intent"
                )
            complete = True

        manual_reason: str | None = None
        if manual_bytes is not None:
            manual_payload = _cleanup_journal_decode(
                manual_bytes,
                path=manual_entry.path / manual_entry.name,
            )
            reason = manual_payload.get("reason")
            if (
                manual_payload.get("version") != 1
                or manual_payload.get("intent_sha256") != intent_sha256
                or not isinstance(reason, str)
                or reason not in _CLEANUP_MANUAL_REVIEW_REASONS
                or set(manual_payload)
                != {"version", "intent_sha256", "reason"}
            ):
                raise ValueError(
                    "invalid storage cleanup manual-review journal"
                )
            manual_reason = reason
        if (
            complete
            and progress_bytes is not None
            and (
                completed_generations != guarded_generations
                or active_generation is not None
            )
        ):
            raise ValueError(
                "storage cleanup completion journal precedes generation progress"
            )
        if complete and manual_reason is not None:
            raise ValueError(
                "storage cleanup journal is both complete and manual-review"
            )
        return _CleanupJournalState(
            intent_sha256=intent_sha256,
            guarded_generations=guarded_generations,
            completed_generations=completed_generations,
            removed_generations=removed_generations,
            active_generation=active_generation,
            complete=complete,
            manual_review_reason=manual_reason,
        )
    finally:
        parent_context.__exit__(None, None, None)


def _create_cleanup_journal(
    settings: Settings,
    candidate: _PendingFileCleanup,
    guarded_generations: frozenset[tuple[int, int]],
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> _CleanupJournalState:
    payload = _cleanup_journal_encode(
        _cleanup_journal_intent_payload(
            candidate,
            guarded_generations,
        )
    )
    with _open_cleanup_journal_parent(
        settings,
        candidate.object_id,
        create=True,
        maintenance_budget=maintenance_budget,
    ) as parent:
        _cleanup_journal_write(
            _cleanup_journal_entry(
                parent,
                candidate.object_id,
                "intent",
            ),
            payload,
            maintenance_budget=maintenance_budget,
            phase="storage cleanup intent journal publication",
        )
    return _CleanupJournalState(
        intent_sha256=hashlib.sha256(payload).hexdigest(),
        guarded_generations=guarded_generations,
        completed_generations=frozenset(),
        removed_generations=frozenset(),
        active_generation=None,
        complete=False,
        manual_review_reason=None,
    )


def _mark_cleanup_journal_progress(
    settings: Settings,
    candidate: _PendingFileCleanup,
    journal: _CleanupJournalState,
    *,
    completed_generations: frozenset[tuple[int, int]],
    removed_generations: frozenset[tuple[int, int]],
    active_generation: tuple[int, int] | None,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
) -> _CleanupJournalState:
    if (
        not completed_generations.issubset(journal.guarded_generations)
        or not removed_generations.issubset(completed_generations)
        or active_generation in completed_generations
        or (
            active_generation is not None
            and active_generation not in journal.guarded_generations
        )
    ):
        raise ValueError("invalid storage cleanup progress transition")
    payload = _cleanup_journal_encode(
        {
            "version": 1,
            "intent_sha256": journal.intent_sha256,
            "completed_generations": [
                {"device": device, "inode": inode}
                for device, inode in sorted(completed_generations)
            ],
            "removed_generations": [
                {"device": device, "inode": inode}
                for device, inode in sorted(removed_generations)
            ],
            "active_generation": (
                None
                if active_generation is None
                else {
                    "device": active_generation[0],
                    "inode": active_generation[1],
                }
            ),
        }
    )
    with _open_cleanup_journal_parent(
        settings,
        candidate.object_id,
    ) as parent:
        _cleanup_journal_write(
            _cleanup_journal_entry(
                parent,
                candidate.object_id,
                "progress",
            ),
            payload,
            maintenance_budget=maintenance_budget,
            mutations_precharged=mutations_precharged,
            replace_existing=True,
            phase="storage cleanup progress journal publication",
        )
    return replace(
        journal,
        completed_generations=completed_generations,
        removed_generations=removed_generations,
        active_generation=active_generation,
    )


def _mark_cleanup_journal_complete(
    settings: Settings,
    candidate: _PendingFileCleanup,
    journal: _CleanupJournalState,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
) -> None:
    payload = _cleanup_journal_encode(
        {
            "version": 1,
            "intent_sha256": journal.intent_sha256,
        }
    )
    with _open_cleanup_journal_parent(
        settings,
        candidate.object_id,
    ) as parent:
        _cleanup_journal_write(
            _cleanup_journal_entry(
                parent,
                candidate.object_id,
                "complete",
            ),
            payload,
            maintenance_budget=maintenance_budget,
            mutations_precharged=mutations_precharged,
            phase="storage cleanup completion journal publication",
        )


def _mark_cleanup_journal_manual_review(
    settings: Settings,
    candidate: _PendingFileCleanup,
    reason: str,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
) -> None:
    journal = _read_cleanup_journal(settings, candidate)
    if journal is None or journal.complete:
        return
    if journal.manual_review_reason is not None:
        if journal.manual_review_reason != reason:
            raise ValueError(
                "storage cleanup journal has a different manual-review reason"
            )
        return
    payload = _cleanup_journal_encode(
        {
            "version": 1,
            "intent_sha256": journal.intent_sha256,
            "reason": reason,
        }
    )
    with _open_cleanup_journal_parent(
        settings,
        candidate.object_id,
    ) as parent:
        _cleanup_journal_write(
            _cleanup_journal_entry(
                parent,
                candidate.object_id,
                "manual-review",
            ),
            payload,
            maintenance_budget=maintenance_budget,
            mutations_precharged=mutations_precharged,
            phase="storage cleanup manual-review journal publication",
        )


def _remove_cleanup_journal(
    settings: Settings,
    object_id: uuid.UUID,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> None:
    try:
        parent_context = _open_cleanup_journal_parent(
            settings,
            object_id,
        )
        parent = parent_context.__enter__()
    except FileNotFoundError:
        return
    try:
        for state in ("complete", "manual-review", "progress", "intent"):
            _cleanup_journal_unlink(
                _cleanup_journal_entry(
                    parent,
                    object_id,
                    state,
                ),
                maintenance_budget=maintenance_budget,
            )
    finally:
        parent_context.__exit__(None, None, None)


def _cleanup_journal_directory_identity(
    parent: _CleanupParent,
) -> tuple[int, int]:
    metadata = (
        parent.path.stat()
        if parent.descriptor is None  # pragma: no cover - Windows
        else os.fstat(parent.descriptor)
    )
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(
            "storage cleanup journal parent must remain a directory"
        )
    return metadata.st_dev, metadata.st_ino


def _fresh_cleanup_journal_scan_cursor(
    parent: _CleanupParent,
) -> _CleanupJournalScanCursor:
    device, inode = _cleanup_journal_directory_identity(parent)
    return _CleanupJournalScanCursor(
        directory_device=device,
        directory_inode=inode,
        offset=0,
        batch_index=0,
    )


def _read_cleanup_journal_scan_cursor(
    settings: Settings,
    parent: _CleanupParent,
) -> tuple[_CleanupJournalScanCursor, str | None]:
    fresh = _fresh_cleanup_journal_scan_cursor(parent)
    try:
        with _open_discovery_data_root(settings) as root_descriptor:
            with _open_discovery_directory(
                root_descriptor,
                settings.data_dir,
                (".staging",),
            ) as control_descriptor:
                _secure_discovery_control_directory(control_descriptor)
                descriptor = os.open(
                    _CLEANUP_JOURNAL_CURSOR_NAME,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=control_descriptor,
                )
                try:
                    metadata = os.fstat(descriptor)
                    current_uid = getattr(
                        os,
                        "geteuid",
                        lambda: metadata.st_uid,
                    )()
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != current_uid
                        or stat.S_IMODE(metadata.st_mode) & 0o077
                        or metadata.st_size
                        > _CLEANUP_JOURNAL_CURSOR_MAX_BYTES
                    ):
                        raise ValueError(
                            "storage cleanup journal cursor must be a small "
                            "owner-only regular file"
                        )
                    payload = bytearray()
                    while (
                        len(payload)
                        <= _CLEANUP_JOURNAL_CURSOR_MAX_BYTES
                    ):
                        chunk = os.read(descriptor, 4096)
                        if not chunk:
                            break
                        payload.extend(chunk)
                    if (
                        len(payload)
                        > _CLEANUP_JOURNAL_CURSOR_MAX_BYTES
                    ):
                        raise ValueError(
                            "storage cleanup journal cursor exceeds its "
                            "size limit"
                        )
                finally:
                    os.close(descriptor)
    except (
        _DiscoveryAncestorMissing,
        FileNotFoundError,
    ):
        return fresh, None
    except (OSError, ValueError) as exc:
        return (
            fresh,
            f"invalid storage cleanup journal cursor: {exc}",
        )

    try:
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "batch_index",
                "directory_device",
                "directory_inode",
                "offset",
                "version",
            }
            or value.get("version") != 1
        ):
            raise ValueError("unsupported storage cleanup journal cursor")
        integers = {
            key: value.get(key)
            for key in (
                "batch_index",
                "directory_device",
                "directory_inode",
                "offset",
            )
        }
        if any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            for item in integers.values()
        ) or (
            integers["offset"] > 2**63 - 1
            or integers["batch_index"] > 4096
        ):
            raise ValueError("invalid storage cleanup journal cursor position")
        if _cleanup_journal_encode(value) != bytes(payload):
            raise ValueError(
                "non-canonical storage cleanup journal cursor"
            )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return (
            fresh,
            f"invalid storage cleanup journal cursor: {exc}",
        )

    if (
        integers["directory_device"],
        integers["directory_inode"],
    ) != (
        fresh.directory_device,
        fresh.directory_inode,
    ):
        return fresh, None
    return (
        _CleanupJournalScanCursor(
            directory_device=integers["directory_device"],
            directory_inode=integers["directory_inode"],
            offset=integers["offset"],
            batch_index=integers["batch_index"],
        ),
        None,
    )


def _write_cleanup_journal_scan_cursor(
    settings: Settings,
    cursor: _CleanupJournalScanCursor,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
) -> None:
    if maintenance_budget is not None and mutations_precharged:
        raise ValueError(
            "storage cleanup journal cursor mutations cannot be both "
            "budgeted and precharged"
        )
    if maintenance_budget is not None:
        maintenance_budget.reserve_directory_entries(
            3,
            phase="storage cleanup journal cursor publication",
            operation="mutation",
        )
    payload = _cleanup_journal_encode(
        {
            "batch_index": cursor.batch_index,
            "directory_device": cursor.directory_device,
            "directory_inode": cursor.directory_inode,
            "offset": cursor.offset,
            "version": 1,
        }
    )
    temporary = (
        f"{_CLEANUP_JOURNAL_CURSOR_NAME}.tmp-{uuid.uuid4().hex}"
    )
    with _open_discovery_data_root(settings) as root_descriptor:
        with _open_discovery_directory(
            root_descriptor,
            settings.data_dir,
            (".staging",),
            create=False,
        ) as control_descriptor:
            _secure_discovery_control_directory(control_descriptor)
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=control_descriptor,
            )
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError(
                            "storage cleanup journal cursor write made no "
                            "progress"
                        )
                    offset += written
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=control_descriptor)
                except FileNotFoundError:
                    pass
                raise
            finally:
                os.close(descriptor)
            try:
                os.replace(
                    temporary,
                    _CLEANUP_JOURNAL_CURSOR_NAME,
                    src_dir_fd=control_descriptor,
                    dst_dir_fd=control_descriptor,
                )
                os.fsync(control_descriptor)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=control_descriptor)
                except FileNotFoundError:
                    pass
                raise


def _reserve_cleanup_journal_cursor_publication(
    settings: Settings,
    maintenance_budget: MaintenanceBudget | None,
) -> None:
    try:
        with _open_discovery_data_root(settings) as root_descriptor:
            with _open_discovery_directory(
                root_descriptor,
                settings.data_dir,
                (".staging",),
            ) as control_descriptor:
                _secure_discovery_control_directory(control_descriptor)
    except _DiscoveryAncestorMissing:
        if maintenance_budget is not None:
            maintenance_budget.consume_directory_entry(
                phase="storage cleanup journal cursor directory creation",
                operation="mkdir",
            )
        with _open_discovery_data_root(settings) as root_descriptor:
            with _open_discovery_directory(
                root_descriptor,
                settings.data_dir,
                (".staging",),
                create=True,
            ) as control_descriptor:
                _secure_discovery_control_directory(control_descriptor)
    if maintenance_budget is not None:
        maintenance_budget.reserve_directory_entries(
            3,
            phase="storage cleanup journal cursor publication",
            operation="mutation",
        )


def _scan_cleanup_journal_names(
    settings: Settings,
    parent: _CleanupParent,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[
    tuple[str, ...],
    bool,
    tuple[str, ...],
    MaintenanceBudgetExceeded | None,
]:
    cursor, cursor_error = _read_cleanup_journal_scan_cursor(
        settings,
        parent,
    )
    errors = [] if cursor_error is None else [cursor_error]
    _reserve_cleanup_journal_cursor_publication(
        settings,
        maintenance_budget,
    )

    if parent.descriptor is None:  # pragma: no cover - Windows
        names = tuple(sorted(entry.name for entry in os.scandir(parent.path)))
        start = min(cursor.offset, len(names))
        selected = names[start : start + _CLEANUP_JOURNAL_SCAN_LIMIT]
        selected_positions = tuple(
            (
                _fresh_cleanup_journal_scan_cursor(parent)
                if start + index >= len(names)
                else _CleanupJournalScanCursor(
                    directory_device=cursor.directory_device,
                    directory_inode=cursor.directory_inode,
                    offset=start + index,
                    batch_index=0,
                ),
                start + index >= len(names),
            )
            for index in range(1, len(selected) + 1)
        )
        complete = not selected or selected_positions[-1][1]
        next_cursor = (
            selected_positions[-1][0]
            if selected_positions
            else _fresh_cleanup_journal_scan_cursor(parent)
        )
    else:
        selected_names: list[str] = []
        selected_positions_list: list[
            tuple[_CleanupJournalScanCursor, bool]
        ] = []
        offset = cursor.offset
        batch_index = cursor.batch_index
        complete = False
        reset_after_invalid_position = False
        next_cursor = cursor
        while len(selected_names) < _CLEANUP_JOURNAL_SCAN_LIMIT:
            try:
                entries, next_offset, batch_complete = read_directory_batch(
                    parent.descriptor,
                    offset,
                )
            except OSError as exc:
                if (
                    (offset != 0 or batch_index != 0)
                    and not reset_after_invalid_position
                ):
                    errors.append(
                        "storage cleanup journal cursor could not resume; "
                        "restarting its bounded scan"
                    )
                    cursor = _fresh_cleanup_journal_scan_cursor(parent)
                    offset = cursor.offset
                    batch_index = cursor.batch_index
                    reset_after_invalid_position = True
                    continue
                errors.append(
                    "could not scan storage cleanup journal directory: "
                    f"{exc}"
                )
                return (
                    tuple(selected_names),
                    True,
                    tuple(errors),
                    None,
                )
            if batch_index > len(entries):
                if reset_after_invalid_position:
                    errors.append(
                        "storage cleanup journal cursor remained invalid "
                        "after restart"
                    )
                    return (
                        tuple(selected_names),
                        True,
                        tuple(errors),
                        None,
                    )
                errors.append(
                    "storage cleanup journal directory changed; restarting "
                    "its bounded scan"
                )
                cursor = _fresh_cleanup_journal_scan_cursor(parent)
                offset = cursor.offset
                batch_index = cursor.batch_index
                reset_after_invalid_position = True
                continue

            remaining = _CLEANUP_JOURNAL_SCAN_LIMIT - len(selected_names)
            for name in entries[batch_index : batch_index + remaining]:
                selected_names.append(name)
                batch_index += 1
                if batch_index < len(entries):
                    position = _CleanupJournalScanCursor(
                        directory_device=cursor.directory_device,
                        directory_inode=cursor.directory_inode,
                        offset=offset,
                        batch_index=batch_index,
                    )
                    position_complete = False
                elif batch_complete:
                    position = _fresh_cleanup_journal_scan_cursor(parent)
                    position_complete = True
                else:
                    position = _CleanupJournalScanCursor(
                        directory_device=cursor.directory_device,
                        directory_inode=cursor.directory_inode,
                        offset=next_offset,
                        batch_index=0,
                    )
                    position_complete = False
                selected_positions_list.append(
                    (position, position_complete)
                )
            if batch_index < len(entries):
                next_cursor = selected_positions_list[-1][0]
                break
            if batch_complete:
                complete = True
                next_cursor = _fresh_cleanup_journal_scan_cursor(parent)
                break
            offset = next_offset
            batch_index = 0
            next_cursor = _CleanupJournalScanCursor(
                directory_device=cursor.directory_device,
                directory_inode=cursor.directory_inode,
                offset=offset,
                batch_index=0,
            )
        selected = tuple(selected_names)
        selected_positions = tuple(selected_positions_list)

    visible_names: list[str] = []
    persisted_cursor = cursor
    persisted_complete = False
    budget_error: MaintenanceBudgetExceeded | None = None
    for name, (position, position_complete) in zip(
        selected,
        selected_positions,
        strict=True,
    ):
        try:
            if maintenance_budget is not None:
                maintenance_budget.consume_directory_entry(
                    phase="storage cleanup journal reconciliation scan",
                    operation="scan",
                )
            if _CLEANUP_JOURNAL_TEMP_NAME.fullmatch(name) is None:
                visible_names.append(name)
            else:
                _cleanup_journal_unlink(
                    _CleanupParent(
                        path=parent.path,
                        name=name,
                        descriptor=parent.descriptor,
                    ),
                    maintenance_budget=maintenance_budget,
                )
        except MaintenanceBudgetExceeded as exc:
            budget_error = exc
            break
        except OSError as exc:
            errors.append(
                "could not retire abandoned storage cleanup journal "
                f"temporary entry {name}: {exc}"
            )
        persisted_cursor = position
        persisted_complete = position_complete
    selected = tuple(visible_names)

    try:
        _write_cleanup_journal_scan_cursor(
            settings,
            (
                persisted_cursor
                if budget_error is not None
                else next_cursor
            ),
            mutations_precharged=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(
            "could not persist storage cleanup journal cursor: "
            f"{exc}"
        )
    return (
        selected,
        not (complete or persisted_complete),
        tuple(errors),
        budget_error,
    )


def _reconcile_completed_cleanup_journals(
    session: Session,
    settings: Settings,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[str, ...]:
    """Retire bounded crash-left journals only after DB completion is durable."""

    try:
        parent_context = _open_cleanup_journal_parent(
            settings,
            uuid.UUID(int=0),
        )
        parent = parent_context.__enter__()
    except FileNotFoundError:
        return ()
    except _CleanupAncestorUnavailable as exc:
        return (str(exc),)
    try:
        (
            names,
            truncated,
            scan_errors,
            scan_budget_error,
        ) = _scan_cleanup_journal_names(
            settings,
            parent,
            maintenance_budget=maintenance_budget,
        )
    finally:
        parent_context.__exit__(None, None, None)

    errors = list(scan_errors)
    if truncated:
        errors.append(
            "storage cleanup journal reconciliation was truncated at "
            f"{_CLEANUP_JOURNAL_SCAN_LIMIT} entries"
        )

    object_ids: set[uuid.UUID] = set()
    for name in sorted(names):
        match = _CLEANUP_JOURNAL_NAME.fullmatch(name)
        if match is None:
            if name.startswith(_DURABLE_UNLINK_QUARANTINE_PREFIX):
                continue
            errors.append(
                "unknown storage cleanup journal entry preserved: "
                f"{_CLEANUP_JOURNAL_DIRECTORY}/{name}"
            )
            continue
        object_ids.add(uuid.UUID(hex=match.group("object_id")))

    removed_journal = False
    removal_budget_error: MaintenanceBudgetExceeded | None = None
    for object_id in sorted(object_ids, key=str):
        obj = session.get(StorageObject, object_id)
        if obj is None:
            errors.append(
                "storage cleanup journal has no matching StorageObject: "
                f"{object_id}"
            )
            continue
        if obj.file_cleanup_completed_at is not None:
            try:
                _remove_cleanup_journal(
                    settings,
                    object_id,
                    maintenance_budget=maintenance_budget,
                )
                removed_journal = True
            except MaintenanceBudgetExceeded as exc:
                removal_budget_error = exc
                break
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(
                    "could not retire completed storage cleanup journal "
                    f"{object_id}: {exc}"
                )
            continue
        if obj.purged_at is None:
            errors.append(
                "storage cleanup journal belongs to an active StorageObject: "
                f"{object_id}"
            )
    if removed_journal:
        try:
            with _open_cleanup_journal_parent(
                settings,
                uuid.UUID(int=0),
            ) as parent:
                _write_cleanup_journal_scan_cursor(
                    settings,
                    _fresh_cleanup_journal_scan_cursor(parent),
                    maintenance_budget=maintenance_budget,
                )
        except MaintenanceBudgetExceeded:
            raise
        except (
            FileNotFoundError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            errors.append(
                "could not reset storage cleanup journal cursor after "
                f"deleting entries: {exc}"
            )
    if removal_budget_error is not None:
        raise removal_budget_error
    if scan_budget_error is not None:
        raise scan_budget_error
    return tuple(errors)


def _cleanup_quarantine_prefix(name: str) -> str:
    digest = hashlib.sha256(os.fsencode(name)).hexdigest()[:20]
    return f"{_CLEANUP_QUARANTINE_PREFIX}{digest}-"


def _iter_cleanup_directory_names(
    parent: _CleanupParent,
    *,
    maintenance_budget: MaintenanceBudget | None,
    phase: str,
) -> Iterator[str]:
    """Stream one directory without materializing unbounded names first."""

    if parent.descriptor is None:  # pragma: no cover - Windows fails closed
        with os.scandir(parent.path) as entries:
            for entry in entries:
                if maintenance_budget is not None:
                    maintenance_budget.consume_directory_entry(
                        phase=phase,
                        operation="scan",
                    )
                yield entry.name
        return

    offset = 0
    while True:
        if maintenance_budget is not None:
            maintenance_budget.checkpoint(phase=phase)
        names, next_offset, complete = read_directory_batch(
            parent.descriptor,
            offset,
        )
        for name in names:
            if maintenance_budget is not None:
                maintenance_budget.consume_directory_entry(
                    phase=phase,
                    operation="scan",
                )
            yield name
        if complete:
            return
        offset = next_offset


def _precharge_cleanup_mutations(
    maintenance_budget: MaintenanceBudget | None,
    count: int,
    *,
    phase: str,
) -> None:
    """Charge a small completion capsule before its first mutation."""

    if maintenance_budget is None:
        return
    maintenance_budget.reserve_directory_entries(
        count,
        phase=phase,
        operation="mutation",
    )


def _legacy_unlink_quarantine_names(
    parent: _CleanupParent,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[str, ...]:
    suffix = f"-{parent.name}"
    matches: list[str] = []
    for name in _iter_cleanup_directory_names(
        parent,
        maintenance_budget=maintenance_budget,
        phase="legacy cleanup quarantine scan",
    ):
        if (
            not name.startswith(_DURABLE_UNLINK_QUARANTINE_PREFIX)
            or name.startswith(
                f"{_DURABLE_UNLINK_QUARANTINE_PREFIX}v2-"
            )
            or not name.endswith(suffix)
        ):
            continue
        token = name[
            len(_DURABLE_UNLINK_QUARANTINE_PREFIX) : -len(suffix)
        ]
        if len(token) == 32 and all(
            character in "0123456789abcdef" for character in token
        ):
            matches.append(name)
    return tuple(sorted(matches))


def _list_cleanup_quarantines(
    parent: _CleanupParent,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[str, ...]:
    prefix = _cleanup_quarantine_prefix(parent.name)
    matches: list[str] = []
    for name in _iter_cleanup_directory_names(
        parent,
        maintenance_budget=maintenance_budget,
        phase="retention cleanup quarantine scan",
    ):
        if name.startswith(prefix):
            matches.append(name)
    return tuple(sorted(matches))


def _create_cleanup_quarantine(
    parent: _CleanupParent,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> str:
    if maintenance_budget is not None:
        maintenance_budget.consume_directory_entry(
            phase="retention cleanup quarantine mkdir",
            operation="mutation",
        )
    prefix = _cleanup_quarantine_prefix(parent.name)
    while True:
        name = f"{prefix}{uuid.uuid4().hex}"
        try:
            if parent.descriptor is None:  # pragma: no cover - Windows
                (parent.path / name).mkdir(mode=0o700)
            else:
                os.mkdir(name, mode=0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        _fsync_cleanup_parent(parent)
        return name


@contextmanager
def _open_cleanup_quarantine(
    parent: _CleanupParent,
    name: str,
) -> Iterator[_CleanupQuarantine]:
    path = parent.path / name
    if parent.descriptor is None:  # pragma: no cover - Windows
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"cleanup quarantine must be a real directory: {path}")
        yield _CleanupQuarantine(
            path=path,
            name=name,
            parent_descriptor=None,
            descriptor=None,
        )
        return

    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent.descriptor,
    )
    try:
        yield _CleanupQuarantine(
            path=path,
            name=name,
            parent_descriptor=parent.descriptor,
            descriptor=descriptor,
        )
    finally:
        os.close(descriptor)


def _cleanup_quarantine_entry(
    quarantine: _CleanupQuarantine,
) -> _CleanupParent:
    return _CleanupParent(
        path=quarantine.path,
        name=_CLEANUP_QUARANTINE_ENTRY,
        descriptor=quarantine.descriptor,
    )


def _quarantine_cleanup_entry(
    parent: _CleanupParent,
    quarantine: _CleanupQuarantine,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> None:
    """Atomically detach the currently named generation into private storage."""
    if maintenance_budget is not None:
        maintenance_budget.consume_directory_entry(
            phase="retention cleanup quarantine rename",
            operation="mutation",
        )
    if parent.descriptor is None:  # pragma: no cover - Windows
        os.rename(
            parent.path / parent.name,
            quarantine.path / _CLEANUP_QUARANTINE_ENTRY,
        )
        return
    os.rename(
        parent.name,
        _CLEANUP_QUARANTINE_ENTRY,
        src_dir_fd=parent.descriptor,
        dst_dir_fd=quarantine.descriptor,
    )


def _remove_cleanup_quarantine(
    parent: _CleanupParent,
    name: str,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> None:
    if maintenance_budget is not None:
        maintenance_budget.consume_directory_entry(
            phase="retention cleanup quarantine rmdir",
            operation="mutation",
        )
    if parent.descriptor is None:  # pragma: no cover - Windows
        (parent.path / name).rmdir()
    else:
        os.rmdir(name, dir_fd=parent.descriptor)
    _fsync_cleanup_parent(parent)


def _metadata_generation(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _capture_entry_identity(
    parent: _CleanupParent,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> dict[str, object]:
    metadata = _cleanup_entry_lstat(parent)
    common: dict[str, object] = {
        "version": _FILE_CLEANUP_IDENTITY_VERSION,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
        "nlink": metadata.st_nlink,
    }
    if stat.S_ISLNK(metadata.st_mode):
        target = _cleanup_entry_readlink(parent)
        after = _cleanup_entry_lstat(parent)
        if _metadata_generation(after) != _metadata_generation(metadata):
            raise OSError("cleanup symlink changed while capturing identity")
        return {
            **common,
            "kind": "symlink",
            "link_target_sha256": hashlib.sha256(
                os.fsencode(target)
            ).hexdigest(),
        }
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("cleanup target must be a regular file or symlink")
    if maintenance_budget is not None:
        maintenance_budget.reserve_hash_bytes(
            metadata.st_size,
            phase="retention cleanup identity hash",
        )

    descriptor = _cleanup_entry_open(parent)
    try:
        before = os.fstat(descriptor)
        if _metadata_generation(before) != _metadata_generation(metadata):
            raise OSError("cleanup file changed before identity hashing")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            if maintenance_budget is not None:
                maintenance_budget.checkpoint(
                    phase="retention cleanup identity hash"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = _cleanup_entry_lstat(parent)
    generation = _metadata_generation(metadata)
    if (
        _metadata_generation(after) != generation
        or _metadata_generation(final) != generation
    ):
        raise OSError("cleanup file changed while capturing identity")
    return {
        **common,
        "kind": "regular",
        "sha256": digest.hexdigest(),
    }


def _capture_cleanup_identity(
    settings: Settings,
    relative_path: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    missing_identity: Mapping[str, object] | None = None,
    maintenance_budget: MaintenanceBudget | None = None,
) -> dict[str, object]:
    try:
        with _open_cleanup_parent(settings, relative_path) as parent:
            identity = _capture_entry_identity(
                parent,
                maintenance_budget=maintenance_budget,
            )
    except FileNotFoundError:
        if missing_identity is None:
            identity = {
                "version": _FILE_CLEANUP_IDENTITY_VERSION,
                "kind": "missing",
            }
        else:
            identity = _cleanup_identity_without_aliases(
                _normalize_cleanup_identity(missing_identity)
            )
            identity["version"] = _FILE_CLEANUP_IDENTITY_VERSION
    if identity["kind"] == "regular":
        if expected_size is not None and identity["size"] != expected_size:
            raise ValueError(
                f"indexed size does not match cleanup file: {relative_path}"
            )
        if (
            expected_sha256 is not None
            and identity["sha256"] != expected_sha256.lower()
        ):
            raise ValueError(
                f"indexed SHA-256 does not match cleanup file: {relative_path}"
            )
    aliases: list[dict[str, object]] = []
    same_inode_aliases = 0
    for alias_relative_path in _staging_alias_relative_paths(
        relative_path
    ):
        try:
            with _open_cleanup_parent(
                settings,
                alias_relative_path,
            ) as alias_parent:
                alias_identity = _capture_entry_identity(
                    alias_parent,
                    maintenance_budget=maintenance_budget,
                )
        except (FileNotFoundError, _CleanupAncestorUnavailable):
            continue
        if alias_identity["kind"] != "regular":
            raise ValueError(
                "indexed staging alias is not a regular file: "
                f"{alias_relative_path}"
            )
        if (
            expected_size is not None
            and alias_identity["size"] != expected_size
        ):
            raise ValueError(
                "indexed staging alias size does not match cleanup payload: "
                f"{alias_relative_path}"
            )
        if (
            expected_sha256 is not None
            and alias_identity["sha256"] != expected_sha256.lower()
        ):
            raise ValueError(
                "indexed staging alias SHA-256 does not match cleanup payload: "
                f"{alias_relative_path}"
            )
        same_inode = (
            identity["kind"] == "regular"
            and alias_identity["device"] == identity["device"]
            and alias_identity["inode"] == identity["inode"]
        )
        if same_inode:
            same_inode_aliases += 1
        elif alias_identity["nlink"] != 1:
            raise ValueError(
                "indexed staging alias has unknown hard links: "
                f"{alias_relative_path}"
            )
        aliases.append(
            {
                "relative_path": alias_relative_path,
                "identity": alias_identity,
            }
        )
    if (
        identity["kind"] == "regular"
        and identity["nlink"] != 1 + same_inode_aliases
    ):
        raise ValueError(
            "cleanup payload has unknown hard links outside its indexed "
            f"staging aliases: {relative_path}"
        )
    if identity["kind"] == "symlink" and aliases:
        raise ValueError(
            "symlink cleanup payload unexpectedly has staging aliases: "
            f"{relative_path}"
        )
    identity["aliases"] = aliases
    return identity


def _staging_alias_relative_paths(
    relative_path: str,
) -> tuple[str, ...]:
    parts = _validate_cleanup_relative_path(relative_path)
    if parts[0] == "media" and len(parts) == 4:
        return (
            "/".join((".staging", *parts)) + ".part",
        )
    if parts[0] == "raw_ingest" and len(parts) == 5:
        return (
            "/".join((".staging", *parts)) + ".part",
        )
    return ()


def _identity_integer(
    value: Mapping[str, object],
    key: str,
    *,
    minimum: int,
) -> int:
    candidate = value.get(key)
    if (
        isinstance(candidate, bool)
        or not isinstance(candidate, int)
        or candidate < minimum
    ):
        raise ValueError(f"invalid file cleanup identity field: {key}")
    return candidate


def _normalize_cleanup_identity(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("missing file cleanup identity")
    version = value.get("version")
    if version not in {
        _LEGACY_FILE_CLEANUP_IDENTITY_VERSION,
        _FILE_CLEANUP_IDENTITY_VERSION,
    }:
        raise ValueError("unsupported file cleanup identity version")
    kind = value.get("kind")
    if kind == "missing":
        return {
            "version": version,
            "kind": "missing",
            **(
                {"aliases": _normalize_cleanup_aliases(value)}
                if version == _FILE_CLEANUP_IDENTITY_VERSION
                else {}
            ),
        }
    if kind not in {"regular", "symlink"}:
        raise ValueError("invalid file cleanup identity kind")
    normalized: dict[str, object] = {
        "version": version,
        "kind": kind,
        "device": _identity_integer(value, "device", minimum=0),
        "inode": _identity_integer(value, "inode", minimum=0),
        "size": _identity_integer(value, "size", minimum=0),
        "mtime_ns": _identity_integer(value, "mtime_ns", minimum=0),
        "ctime_ns": _identity_integer(value, "ctime_ns", minimum=0),
        "nlink": _identity_integer(value, "nlink", minimum=1),
    }
    digest_key = (
        "sha256" if kind == "regular" else "link_target_sha256"
    )
    digest = value.get(digest_key)
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(
        digest.lower()
    ):
        raise ValueError(
            f"invalid file cleanup identity field: {digest_key}"
        )
    normalized[digest_key] = digest.lower()
    if version == _FILE_CLEANUP_IDENTITY_VERSION:
        normalized["aliases"] = _normalize_cleanup_aliases(value)
        manual_review = value.get("manual_review_required")
        if manual_review is not None:
            if manual_review not in _CLEANUP_MANUAL_REVIEW_REASONS:
                raise ValueError(
                    "invalid file cleanup manual review reason"
                )
            normalized["manual_review_required"] = manual_review
    return normalized


def _normalize_cleanup_aliases(
    value: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_aliases = value.get("aliases", [])
    if not isinstance(raw_aliases, list):
        raise ValueError("invalid file cleanup identity aliases")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_alias in raw_aliases:
        if not isinstance(raw_alias, Mapping):
            raise ValueError("invalid file cleanup alias")
        relative_path = raw_alias.get("relative_path")
        if not isinstance(relative_path, str):
            raise ValueError("invalid file cleanup alias path")
        _validate_cleanup_relative_path(relative_path)
        if relative_path in seen:
            raise ValueError("duplicate file cleanup alias path")
        seen.add(relative_path)
        identity = _normalize_cleanup_identity(
            raw_alias.get("identity")
        )
        if identity.get("aliases"):
            raise ValueError("nested file cleanup aliases are not allowed")
        normalized.append(
            {
                "relative_path": relative_path,
                "identity": identity,
            }
        )
    return normalized


def _upgrade_legacy_cleanup_identity(
    settings: Settings,
    obj: StorageObject,
    legacy: Mapping[str, object],
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> dict[str, object]:
    """Re-prove deterministic aliases before converting a v1 retry to v2.

    A v1 durable unlink may already have moved the final path into its legacy
    quarantine. Preserve the recorded final generation in the v2 identity so
    post-commit cleanup can still locate and remove that quarantine.
    """
    expected_sha256 = (
        str(legacy["sha256"])
        if legacy.get("kind") == "regular"
        else None
    )
    current = _capture_cleanup_identity(
        settings,
        obj.relative_path,
        expected_size=obj.size_bytes,
        expected_sha256=expected_sha256,
        missing_identity=legacy,
        maintenance_budget=maintenance_budget,
    )
    current_final = _cleanup_identity_without_aliases(current)
    if (
        current_final.get("kind") != "missing"
        and not _named_cleanup_identity_matches(current_final, legacy)
    ):
        raise ValueError(
            "legacy cleanup path no longer names its recorded generation"
        )
    aliases = current.get("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("invalid upgraded cleanup aliases")
    for alias in aliases:
        if not isinstance(alias, Mapping):
            raise ValueError("invalid upgraded cleanup alias")
        alias_identity = alias.get("identity")
        if not isinstance(alias_identity, Mapping):
            raise ValueError("invalid upgraded cleanup alias identity")
        if (
            legacy.get("kind") != "regular"
            or alias_identity.get("kind") != "regular"
            or alias_identity.get("size") != legacy.get("size")
            or alias_identity.get("sha256") != legacy.get("sha256")
        ):
            raise ValueError(
                "legacy staging alias does not match the recorded payload "
                "generation"
            )
    return _normalize_cleanup_identity(current)


def _capture_pre_identity_cleanup(
    settings: Settings,
    obj: StorageObject,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> dict[str, object]:
    """Prove a purged row created before cleanup identities existed.

    A missing path can be acknowledged. Existing bytes are eligible only when
    the legacy index has a valid SHA-256 and the current regular file (plus any
    deterministic staging alias) matches both that digest and ``size_bytes``.
    Symlinks and unverifiable bytes stay pending instead of being deleted.
    """
    indexed_sha256 = obj.sha256
    invalid_indexed_sha256 = False
    if indexed_sha256 is not None:
        if (
            not isinstance(indexed_sha256, str)
            or not _SHA256_HEX.fullmatch(indexed_sha256.lower())
        ):
            invalid_indexed_sha256 = True
            expected_sha256 = None
        else:
            expected_sha256 = indexed_sha256.lower()
    else:
        expected_sha256 = None

    identity = _capture_cleanup_identity(
        settings,
        obj.relative_path,
        expected_size=obj.size_bytes,
        expected_sha256=expected_sha256,
        maintenance_budget=maintenance_budget,
    )
    with _open_cleanup_parent(settings, obj.relative_path) as parent:
        legacy_quarantines = _legacy_unlink_quarantine_names(
            parent,
            maintenance_budget=maintenance_budget,
        )
    if legacy_quarantines:
        raise ValueError(
            "legacy durable-unlink quarantine still contains an "
            "unverified payload; preserving it for manual review: "
            + ", ".join(legacy_quarantines)
        )
    final_identity = _cleanup_identity_without_aliases(identity)
    aliases = identity.get("aliases", [])
    if final_identity.get("kind") == "symlink":
        raise ValueError(
            "legacy purged cleanup target is a symlink; preserving it"
        )
    if invalid_indexed_sha256 and (
        final_identity.get("kind") != "missing" or aliases
    ):
        raise ValueError(
            "legacy purged object has existing bytes but an invalid indexed "
            "SHA-256; preserving them for manual review"
        )
    if expected_sha256 is None and (
        final_identity.get("kind") != "missing" or aliases
    ):
        raise ValueError(
            "legacy purged object has existing bytes but no indexed SHA-256; "
            "preserving them for manual review"
        )
    return _normalize_cleanup_identity(identity)


def _cleanup_identity_without_aliases(
    identity: Mapping[str, object],
) -> dict[str, object]:
    return {
        key: value
        for key, value in identity.items()
        if key != "aliases"
    }


def _cleanup_identity_without_manual_review(
    identity: Mapping[str, object],
) -> dict[str, object]:
    return {
        key: value
        for key, value in identity.items()
        if key != "manual_review_required"
    }


def _named_cleanup_identity_matches(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    """Match one name after known sibling hard links may have been removed."""
    if current.get("kind") != expected.get("kind"):
        return False
    if current.get("kind") == "missing":
        return True
    common = (
        "kind",
        "device",
        "inode",
        "size",
        "mtime_ns",
    )
    if any(current.get(key) != expected.get(key) for key in common):
        return False
    current_nlink = current.get("nlink")
    expected_nlink = expected.get("nlink")
    if (
        not isinstance(current_nlink, int)
        or not isinstance(expected_nlink, int)
        or current_nlink < 1
        or current_nlink > expected_nlink
    ):
        return False
    digest_key = (
        "sha256"
        if expected.get("kind") == "regular"
        else "link_target_sha256"
    )
    return current.get(digest_key) == expected.get(digest_key)


def _quarantined_cleanup_identity_matches(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    """Compare a moved generation while allowing rename-induced ctime changes."""
    return (
        expected["kind"] != "missing"
        and _named_cleanup_identity_matches(current, expected)
    )


def _same_cleanup_object(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
    )


def _restore_quarantined_cleanup_entry(
    parent: _CleanupParent,
    quarantine: _CleanupQuarantine,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> bool:
    """Restore without clobbering a newer name, or leave quarantine intact."""
    entry = _cleanup_quarantine_entry(quarantine)
    quarantined_metadata = _cleanup_entry_lstat(entry)
    try:
        current_metadata = _cleanup_entry_lstat(parent)
    except FileNotFoundError:
        current_metadata = None

    if current_metadata is not None:
        if not _same_cleanup_object(
            current_metadata,
            quarantined_metadata,
        ):
            return False
        if maintenance_budget is not None:
            maintenance_budget.consume_directory_entry(
                phase="retention cleanup duplicate quarantine unlink",
                operation="mutation",
            )
        _unlink_cleanup_entry(entry)
        _fsync_cleanup_parent(entry)
        return True

    try:
        if maintenance_budget is not None:
            maintenance_budget.consume_directory_entry(
                phase="retention cleanup quarantine unlink",
                operation="mutation",
            )
        if parent.descriptor is None:  # pragma: no cover - Windows
            os.link(
                quarantine.path / _CLEANUP_QUARANTINE_ENTRY,
                parent.path / parent.name,
                follow_symlinks=False,
            )
        else:
            os.link(
                _CLEANUP_QUARANTINE_ENTRY,
                parent.name,
                src_dir_fd=quarantine.descriptor,
                dst_dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
    except FileExistsError:
        return False
    except OSError:
        logger.warning(
            "could not restore raced cleanup generation from %s",
            quarantine.path,
            exc_info=True,
        )
        return False

    # Persist the restored name before retiring its quarantine hard link.
    _fsync_cleanup_parent(parent)
    if maintenance_budget is not None:
        maintenance_budget.consume_directory_entry(
            phase="retention cleanup quarantine unlink",
            operation="mutation",
        )
    _unlink_cleanup_entry(entry)
    _fsync_cleanup_parent(entry)
    return True


def _recover_cleanup_quarantines(
    parent: _CleanupParent,
    expected: Mapping[str, object],
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> bool:
    """Finish a crash-interrupted move/delete before touching the live name."""
    reclaimed_expected = False
    for name in _list_cleanup_quarantines(
        parent,
        maintenance_budget=maintenance_budget,
    ):
        remove_directory = False
        retirement_precharged = False
        mismatch_message: str | None = None
        with _open_cleanup_quarantine(parent, name) as quarantine:
            entry = _cleanup_quarantine_entry(quarantine)
            try:
                current = _capture_entry_identity(
                    entry,
                    maintenance_budget=maintenance_budget,
                )
            except FileNotFoundError:
                remove_directory = True
            else:
                if _quarantined_cleanup_identity_matches(current, expected):
                    _precharge_cleanup_mutations(
                        maintenance_budget,
                        2,
                        phase=(
                            "retention cleanup quarantine deletion capsule"
                        ),
                    )
                    _unlink_cleanup_entry(entry)
                    _fsync_cleanup_parent(entry)
                    remove_directory = True
                    retirement_precharged = True
                    reclaimed_expected = True
                elif _restore_quarantined_cleanup_entry(
                    parent,
                    quarantine,
                    maintenance_budget=maintenance_budget,
                ):
                    remove_directory = True
                    mismatch_message = (
                        "file identity changed after purge; restored the "
                        "quarantined generation without overwriting a newer file"
                    )
                else:
                    mismatch_message = (
                        "file identity changed after purge; preserved the "
                        f"quarantined generation at {quarantine.path}"
                    )
        if remove_directory:
            _remove_cleanup_quarantine(
                parent,
                name,
                maintenance_budget=(
                    None
                    if retirement_precharged
                    else maintenance_budget
                ),
            )
        if mismatch_message is not None:
            raise _CleanupIdentityMismatch(mismatch_message)
    return reclaimed_expected


def _cleanup_one_named_file(
    settings: Settings,
    relative_path: str,
    expected: Mapping[str, object],
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> bool:
    removed = False
    try:
        with _open_cleanup_parent(
            settings,
            relative_path,
        ) as parent:
            recovered_storage_quarantine = _recover_cleanup_quarantines(
                parent,
                expected,
                maintenance_budget=maintenance_budget,
            )
            if recovered_storage_quarantine:
                removed = True
                current = None
            else:
                try:
                    current = _capture_entry_identity(
                        parent,
                        maintenance_budget=maintenance_budget,
                    )
                except FileNotFoundError:
                    current = None
            if current is not None:
                if not _named_cleanup_identity_matches(current, expected):
                    raise _CleanupIdentityMismatch(
                        "file identity changed after purge; preserving the current "
                        "directory entry"
                    )
                quarantine_name = _create_cleanup_quarantine(
                    parent,
                    maintenance_budget=maintenance_budget,
                )
                remove_directory = False
                retirement_precharged = False
                mismatch_message: str | None = None
                try:
                    with _open_cleanup_quarantine(
                        parent,
                        quarantine_name,
                    ) as quarantine:
                        try:
                            _quarantine_cleanup_entry(
                                parent,
                                quarantine,
                                maintenance_budget=maintenance_budget,
                            )
                        except FileNotFoundError:
                            remove_directory = True
                        else:
                            entry = _cleanup_quarantine_entry(quarantine)
                            # Both sides of the rename are durable before deletion.
                            _fsync_cleanup_parent(entry)
                            _fsync_cleanup_parent(parent)
                            moved = _capture_entry_identity(
                                entry,
                                maintenance_budget=maintenance_budget,
                            )
                            if _quarantined_cleanup_identity_matches(
                                moved,
                                expected,
                            ):
                                _precharge_cleanup_mutations(
                                    maintenance_budget,
                                    2,
                                    phase=(
                                        "retention cleanup quarantine "
                                        "deletion capsule"
                                    ),
                                )
                                _unlink_cleanup_entry(entry)
                                _fsync_cleanup_parent(entry)
                                remove_directory = True
                                retirement_precharged = True
                                removed = True
                            elif _restore_quarantined_cleanup_entry(
                                parent,
                                quarantine,
                                maintenance_budget=maintenance_budget,
                            ):
                                remove_directory = True
                                mismatch_message = (
                                    "file identity changed after verification; "
                                    "restored the raced generation without "
                                    "overwriting a newer file"
                                )
                            else:
                                mismatch_message = (
                                    "file identity changed after verification; "
                                    "preserved the raced generation at "
                                    f"{quarantine.path}"
                                )
                except BaseException:
                    if remove_directory:
                        _remove_cleanup_quarantine(
                            parent,
                            quarantine_name,
                            maintenance_budget=(
                                None
                                if retirement_precharged
                                else maintenance_budget
                            ),
                        )
                    raise
                if remove_directory:
                    _remove_cleanup_quarantine(
                        parent,
                        quarantine_name,
                        maintenance_budget=(
                            None
                            if retirement_precharged
                            else maintenance_budget
                        ),
                    )
                if mismatch_message is not None:
                    raise _CleanupIdentityMismatch(mismatch_message)
    except FileNotFoundError:
        pass

    if expected.get("kind") == "regular" and not removed:
        durable_identity = DurableFileIdentity(
            device=int(expected["device"]),
            inode=int(expected["inode"]),
            size=int(expected["size"]),
            mtime_ns=int(expected["mtime_ns"]),
        )
        durable_removed = durable_unlink(
            settings.data_dir / relative_path,
            missing_ok=True,
            expected=durable_identity,
            budget=maintenance_budget,
        )
        if not durable_removed:
            durable_removed = recover_durable_unlink_target(
                settings.data_dir / relative_path,
                durable_identity,
                budget=maintenance_budget,
            )
        removed = removed or durable_removed
    return removed


def _open_matching_cleanup_descriptor(
    settings: Settings,
    relative_path: str,
    expected: Mapping[str, object],
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> int | None:
    """Hold one known name for a regular generation across unlink.

    The descriptor lets cleanup prove that the inode has no unknown hard links
    after every HealthMes-owned name and quarantine has been removed.
    """

    if expected.get("kind") != "regular":
        return None

    def matching(descriptor: int) -> bool:
        metadata = os.fstat(descriptor)
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == expected.get("device")
            and metadata.st_ino == expected.get("inode")
            and metadata.st_size == expected.get("size")
            and metadata.st_mtime_ns == expected.get("mtime_ns")
        )

    def keep_if_matching(descriptor: int) -> int | None:
        if matching(descriptor):
            return descriptor
        os.close(descriptor)
        return None

    try:
        with _open_cleanup_parent(settings, relative_path) as parent:
            try:
                descriptor = _cleanup_entry_open(parent)
            except FileNotFoundError:
                pass
            else:
                matched = keep_if_matching(descriptor)
                if matched is not None:
                    return matched

            for name in _list_cleanup_quarantines(
                parent,
                maintenance_budget=maintenance_budget,
            ):
                try:
                    with _open_cleanup_quarantine(parent, name) as quarantine:
                        descriptor = _cleanup_entry_open(
                            _cleanup_quarantine_entry(quarantine)
                        )
                except FileNotFoundError:
                    continue
                matched = keep_if_matching(descriptor)
                if matched is not None:
                    return matched

            if os.name == "nt":  # pragma: no cover - Windows fails closed earlier
                return None
            assert parent.descriptor is not None
            suffix = f"-{parent.name}"
            for name in _iter_cleanup_directory_names(
                parent,
                maintenance_budget=maintenance_budget,
                phase="legacy durable unlink quarantine scan",
            ):
                if (
                    name.startswith(_DURABLE_UNLINK_QUARANTINE_PREFIX)
                    and not name.startswith(
                        f"{_DURABLE_UNLINK_QUARANTINE_PREFIX}v2-"
                    )
                    and name.endswith(suffix)
                ):
                    try:
                        descriptor = os.open(
                            name,
                            os.O_RDONLY
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=parent.descriptor,
                        )
                    except FileNotFoundError:
                        continue
                    matched = keep_if_matching(descriptor)
                    if matched is not None:
                        return matched
                    continue
                if not name.startswith(
                    f"{_DURABLE_UNLINK_QUARANTINE_PREFIX}v2-"
                ):
                    continue
                try:
                    quarantine_descriptor = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent.descriptor,
                    )
                except FileNotFoundError:
                    continue
                try:
                    try:
                        descriptor = os.open(
                            "payload",
                            os.O_RDONLY
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=quarantine_descriptor,
                        )
                    except FileNotFoundError:
                        continue
                    matched = keep_if_matching(descriptor)
                    if matched is not None:
                        return matched
                finally:
                    os.close(quarantine_descriptor)
    except FileNotFoundError:
        return None
    return None


def _count_known_cleanup_links(
    settings: Settings,
    entries: list[tuple[str, dict[str, object]]],
    descriptor: int,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> int:
    """Count current HealthMes-owned names for one already-open regular inode."""

    expected = os.fstat(descriptor)
    known_names: set[tuple[str, str, str]] = set()

    def record(
        *,
        parent: _CleanupParent,
        name: str,
        inner_name: str,
        metadata: os.stat_result,
    ) -> None:
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == expected.st_dev
            and metadata.st_ino == expected.st_ino
        ):
            known_names.add((str(parent.path), name, inner_name))

    for relative_path, _identity in entries:
        try:
            with _open_cleanup_parent(settings, relative_path) as parent:
                try:
                    record(
                        parent=parent,
                        name=parent.name,
                        inner_name="",
                        metadata=_cleanup_entry_lstat(parent),
                    )
                except FileNotFoundError:
                    pass

                for name in _list_cleanup_quarantines(
                    parent,
                    maintenance_budget=maintenance_budget,
                ):
                    try:
                        with _open_cleanup_quarantine(parent, name) as quarantine:
                            record(
                                parent=parent,
                                name=name,
                                inner_name=_CLEANUP_QUARANTINE_ENTRY,
                                metadata=_cleanup_entry_lstat(
                                    _cleanup_quarantine_entry(quarantine)
                                ),
                            )
                    except FileNotFoundError:
                        continue

                if os.name == "nt":  # pragma: no cover - Windows fails closed earlier
                    continue
                assert parent.descriptor is not None
                suffix = f"-{parent.name}"
                for name in _iter_cleanup_directory_names(
                    parent,
                    maintenance_budget=maintenance_budget,
                    phase="known cleanup link scan",
                ):
                    if (
                        name.startswith(_DURABLE_UNLINK_QUARANTINE_PREFIX)
                        and not name.startswith(
                            f"{_DURABLE_UNLINK_QUARANTINE_PREFIX}v2-"
                        )
                        and name.endswith(suffix)
                    ):
                        try:
                            metadata = os.stat(
                                name,
                                dir_fd=parent.descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        record(
                            parent=parent,
                            name=name,
                            inner_name="",
                            metadata=metadata,
                        )
                        continue
                    if not name.startswith(
                        f"{_DURABLE_UNLINK_QUARANTINE_PREFIX}v2-"
                    ):
                        continue
                    try:
                        with _open_cleanup_quarantine(
                            parent,
                            name,
                        ) as quarantine:
                            record(
                                parent=parent,
                                name=name,
                                inner_name=_CLEANUP_QUARANTINE_ENTRY,
                                metadata=_cleanup_entry_lstat(
                                    _cleanup_quarantine_entry(quarantine)
                                ),
                            )
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
    return len(known_names)


@contextmanager
def _guard_cleanup_generations(
    settings: Settings,
    entries: list[tuple[str, dict[str, object]]],
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> Iterator[dict[tuple[int, int], int]]:
    """Keep each known regular inode open and require final link count zero."""

    grouped: dict[tuple[int, int], list[tuple[str, dict[str, object]]]] = {}
    for relative_path, identity in entries:
        if identity.get("kind") != "regular":
            continue
        key = (int(identity["device"]), int(identity["inode"]))
        grouped.setdefault(key, []).append((relative_path, identity))

    descriptors: dict[tuple[int, int], int] = {}
    try:
        for key, grouped_entries in grouped.items():
            for relative_path, identity in grouped_entries:
                descriptor = _open_matching_cleanup_descriptor(
                    settings,
                    relative_path,
                    identity,
                    maintenance_budget=maintenance_budget,
                )
                if descriptor is None:
                    continue
                descriptors[key] = descriptor
                expected_nlink = max(
                    int(entry_identity["nlink"])
                    for _, entry_identity in grouped_entries
                )
                if os.fstat(descriptor).st_nlink > expected_nlink:
                    raise _CleanupIdentityMismatch(
                        "cleanup payload has unknown hard links before deletion"
                    )
                known_links = _count_known_cleanup_links(
                    settings,
                    grouped_entries,
                    descriptor,
                    maintenance_budget=maintenance_budget,
                )
                if os.fstat(descriptor).st_nlink != known_links:
                    raise _CleanupIdentityMismatch(
                        "cleanup payload has unknown hard links replacing "
                        "HealthMes-owned names"
                    )
                break
        yield descriptors
        for descriptor in descriptors.values():
            if os.fstat(descriptor).st_nlink != 0:
                raise _CleanupGenerationOrphaned(
                    "cleanup payload still has unknown hard links after "
                    "HealthMes-owned names were removed"
                )
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def _cleanup_one_purged_file(
    settings: Settings,
    candidate: _PendingFileCleanup,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> _CleanupOutcome:
    expected = _normalize_cleanup_identity(candidate.identity)
    manual_review_reason = expected.get("manual_review_required")
    if isinstance(manual_review_reason, str):
        raise _CleanupManualReviewRequired(
            manual_review_reason,
            "file cleanup requires manual review because its prior physical "
            "outcome cannot be proved automatically",
        )
    journal = _read_cleanup_journal(settings, candidate)
    if journal is not None:
        if journal.manual_review_reason is not None:
            raise _CleanupManualReviewRequired(
                journal.manual_review_reason,
                "file cleanup requires manual review because its durable "
                "journal records an ambiguous physical outcome",
            )
        if journal.complete:
            return _CleanupOutcome(bytes_reclaimed=0, files_deleted=0)
    aliases = expected.get("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("invalid normalized file cleanup aliases")
    entries: list[tuple[str, dict[str, object]]] = []
    for alias in aliases:
        if not isinstance(alias, Mapping):
            raise ValueError("invalid normalized file cleanup alias")
        alias_path = alias.get("relative_path")
        alias_identity = alias.get("identity")
        if not isinstance(alias_path, str) or not isinstance(
            alias_identity,
            dict,
        ):
            raise ValueError("invalid normalized file cleanup alias")
        entries.append((alias_path, alias_identity))
    entries.append(
        (
            candidate.relative_path,
            _cleanup_identity_without_aliases(expected),
        )
    )

    groups: dict[
        tuple[int, int],
        list[tuple[str, dict[str, object]]],
    ] = {}
    for relative_path, entry_identity in entries:
        if entry_identity.get("kind") != "regular":
            continue
        key = (
            int(entry_identity["device"]),
            int(entry_identity["inode"]),
        )
        groups.setdefault(key, []).append(
            (relative_path, entry_identity)
        )

    removed_nonregular = False
    terminal_capsules_precharged = False
    try:
        guarded_generations = frozenset(groups)
        if journal is not None:
            if journal.guarded_generations != guarded_generations:
                raise ValueError(
                    "storage cleanup journal generation set does not match "
                    "the pending object"
                )
        elif groups:
            journal = _create_cleanup_journal(
                settings,
                candidate,
                guarded_generations,
                maintenance_budget=maintenance_budget,
            )
        if journal is not None:
            _precharge_cleanup_mutations(
                maintenance_budget,
                6,
                phase="storage cleanup terminal journal capsules",
            )
            terminal_capsules_precharged = True

        completed_generations = (
            journal.completed_generations
            if journal is not None
            else frozenset()
        )
        pending_generations = [
            generation
            for generation in sorted(groups)
            if generation not in completed_generations
        ]
        if (
            journal is not None
            and journal.active_generation in pending_generations
        ):
            active = journal.active_generation
            assert active is not None
            pending_generations.remove(active)
            pending_generations.insert(0, active)

        for generation in pending_generations:
            grouped_entries = groups[generation]
            with _guard_cleanup_generations(
                settings,
                grouped_entries,
                maintenance_budget=maintenance_budget,
            ) as descriptors:
                descriptor = descriptors.get(generation)
                if descriptor is None:
                    raise _CleanupOutcomeUnproven(
                        "cleanup payload has no remaining HealthMes-owned name "
                        "for a recorded generation"
                    )
                if journal is None:
                    raise RuntimeError(
                        "regular cleanup generation is missing its intent journal"
                    )

                # Persist the active generation and reserve its completion
                # publication before touching any of its names. The shared
                # budget remains active for every scan, hash and mutation.
                _precharge_cleanup_mutations(
                    maintenance_budget,
                    6,
                    phase="storage cleanup generation progress capsule",
                )
                journal = _mark_cleanup_journal_progress(
                    settings,
                    candidate,
                    journal,
                    completed_generations=journal.completed_generations,
                    removed_generations=journal.removed_generations,
                    active_generation=generation,
                    mutations_precharged=True,
                )
                generation_removed = False
                for relative_path, entry_identity in grouped_entries:
                    known_links = _count_known_cleanup_links(
                        settings,
                        grouped_entries,
                        descriptor,
                        maintenance_budget=maintenance_budget,
                    )
                    if os.fstat(descriptor).st_nlink != known_links:
                        raise _CleanupIdentityMismatch(
                            "cleanup payload has unknown hard links replacing "
                            "HealthMes-owned names"
                        )
                    if _cleanup_one_named_file(
                        settings,
                        relative_path,
                        entry_identity,
                        maintenance_budget=maintenance_budget,
                    ):
                        generation_removed = True

                # Publish completion while the verified inode is still open.
                # A crash after the last unlink can then resume from this
                # durable marker instead of treating the vanished generation
                # as an ambiguous outcome.
                if os.fstat(descriptor).st_nlink != 0:
                    raise _CleanupGenerationOrphaned(
                        "cleanup payload still has unknown hard links after "
                        "HealthMes-owned names were removed"
                    )
                completed_generations = frozenset(
                    (*journal.completed_generations, generation)
                )
                removed_generations = journal.removed_generations
                if generation_removed:
                    removed_generations = frozenset(
                        (*removed_generations, generation)
                    )
                journal = _mark_cleanup_journal_progress(
                    settings,
                    candidate,
                    journal,
                    completed_generations=completed_generations,
                    removed_generations=removed_generations,
                    active_generation=None,
                    mutations_precharged=True,
                )

        for relative_path, entry_identity in entries:
            if entry_identity.get("kind") == "regular":
                continue
            if _cleanup_one_named_file(
                settings,
                relative_path,
                entry_identity,
                maintenance_budget=maintenance_budget,
            ):
                removed_nonregular = True
    except _CleanupManualReviewRequired as exc:
        if journal is not None and terminal_capsules_precharged:
            _mark_cleanup_journal_manual_review(
                settings,
                candidate,
                exc.reason,
                mutations_precharged=True,
            )
        raise
    if journal is not None:
        _mark_cleanup_journal_complete(
            settings,
            candidate,
            journal,
            mutations_precharged=terminal_capsules_precharged,
        )

    removed_generations = (
        journal.removed_generations
        if journal is not None
        else frozenset()
    )
    reclaimed = sum(
        int(groups[generation][0][1]["size"])
        for generation in removed_generations
    )
    return _CleanupOutcome(
        bytes_reclaimed=reclaimed,
        files_deleted=(
            1
            if removed_generations or removed_nonregular
            else 0
        ),
    )


def _rollback_maintenance_session(session: Session) -> None:
    try:
        session.rollback()
    except Exception:
        logger.exception("failed to roll back storage maintenance session")


@contextmanager
def _guarded_maintenance_session(
    caller_session: Session,
) -> Iterator[Session]:
    """Run PostgreSQL maintenance on the exact globally guarded connection."""
    bind = caller_session.get_bind()
    with global_write_plane_guard(bind) as guard_connection:
        if (
            guard_connection is None
            or bind.dialect.name != "postgresql"
        ):
            yield caller_session
            return
        maintenance_session = Session(
            bind=guard_connection,
            autoflush=False,
        )
        try:
            yield maintenance_session
        finally:
            maintenance_session.close()
            caller_session.expire_all()


def _cleanup_purged_files(
    settings: Settings,
    candidates: tuple[_PendingFileCleanup, ...],
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    budget_status: _MaintenanceBudgetStatus | None = None,
) -> tuple[
    int,
    int,
    tuple[str, ...],
    tuple[_PendingFileCleanup, ...],
    tuple[_ManualReviewCleanup, ...],
]:
    reclaimed = 0
    files_deleted = 0
    errors: list[str] = []
    completed: list[_PendingFileCleanup] = []
    manual_review: list[_ManualReviewCleanup] = []
    for candidate in candidates:
        try:
            outcome = _cleanup_one_purged_file(
                settings,
                candidate,
                maintenance_budget=maintenance_budget,
            )
        except MaintenanceBudgetExceeded as exc:
            if budget_status is not None:
                budget_status.record(exc)
            errors.append(
                f"{candidate.relative_path}: {_budget_error_text(exc)}"
            )
            # A shared budget is an absolute run boundary. Preserve this and
            # all later candidates for a fresh retry rather than consuming
            # more namespace work after exhaustion.
            break
        except _CleanupManualReviewRequired as exc:
            errors.append(f"{candidate.relative_path}: {exc}")
            try:
                _mark_cleanup_journal_manual_review(
                    settings,
                    candidate,
                    exc.reason,
                    maintenance_budget=maintenance_budget,
                )
            except MaintenanceBudgetExceeded as budget_exc:
                if budget_status is not None:
                    budget_status.record(budget_exc)
                errors.append(
                    f"{candidate.relative_path}: "
                    f"{_budget_error_text(budget_exc)}"
                )
                break
            except (OSError, RuntimeError, ValueError) as journal_exc:
                errors.append(
                    f"{candidate.relative_path}: could not persist cleanup "
                    f"manual-review journal: {journal_exc}"
                )
            manual_review.append(
                _ManualReviewCleanup(
                    candidate=candidate,
                    reason=exc.reason,
                )
            )
            continue
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{candidate.relative_path}: {exc}")
            continue
        reclaimed += outcome.bytes_reclaimed
        files_deleted += outcome.files_deleted
        completed.append(candidate)
    return (
        reclaimed,
        files_deleted,
        tuple(errors),
        tuple(completed),
        tuple(manual_review),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _count_pending_file_cleanup(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(StorageObject)
            .where(
                StorageObject.purged_at.is_not(None),
                StorageObject.file_cleanup_completed_at.is_(None),
            )
        )
        or 0
    )


def _strip_legacy_raw_fields(value: object) -> object:
    if isinstance(value, list):
        return [_strip_legacy_raw_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            []
            if key == "warnings"
            else None
            if key
            in {
                "source_text",
                "media_path",
                "evidence_text",
                "note",
            }
            else _strip_legacy_raw_fields(item)
        )
        for key, item in value.items()
    }


def _legacy_raw_texts(value: object) -> list[str]:
    if isinstance(value, list):
        return [
            text
            for item in value
            for text in _legacy_raw_texts(item)
        ]
    if not isinstance(value, dict):
        return []
    texts: list[str] = []
    for key, item in value.items():
        if key == "evidence_text" and isinstance(item, str):
            texts.append(item)
        elif key == "warnings" and isinstance(item, list):
            texts.extend(
                warning
                for warning in item
                if isinstance(warning, str)
            )
        else:
            texts.extend(_legacy_raw_texts(item))
    return list(dict.fromkeys(texts))


def _migrate_legacy_nutrition_raw_captures(
    session: Session,
    *,
    current: datetime,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    raw_policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "nutrition_raw_capture"
        )
    )
    if raw_policy is None:  # pragma: no cover - defaults own this invariant
        return
    raw_record_ids = set(
        session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.source_provider == "nutrition-raw-capture"
            )
        )
    )
    legacy_events = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-interaction"
        )
    )
    for event in legacy_events:
        source_text = event.payload.get("source_text")
        media_path = event.payload.get("media_path")
        warnings = event.payload.get("warnings")
        items = event.payload.get("items")
        item_warnings = (
            [
                (
                    item.get("warnings", [])
                    if isinstance(item, dict)
                    else []
                )
                for item in items
            ]
            if isinstance(items, list)
            else []
        )
        raw_texts = _legacy_raw_texts(event.payload)
        if (
            source_text is None
            and media_path is None
            and not raw_texts
        ):
            continue
        observed_at = _as_utc(event.observed_at)
        expires_at = (
            None
            if not raw_policy.enabled
            or raw_policy.retention_days is None
            else observed_at
            + timedelta(days=raw_policy.retention_days)
        )
        if (
            event.source_record_id not in raw_record_ids
            and (expires_at is None or expires_at > _as_utc(current))
        ):
            raw_object_id = None
            if isinstance(media_path, str):
                obj = session.scalar(
                    select(StorageObject).where(
                        StorageObject.relative_path == media_path
                    )
                )
                raw_object_id = obj.id if obj is not None else None
            session.add(
                WellnessEvent(
                    event_type="nutrition.raw-capture.v1",
                    schema_version=1,
                    observed_at=event.observed_at,
                    recorded_at=event.recorded_at,
                    timezone=event.timezone,
                    source_provider="nutrition-raw-capture",
                    source_device=event.source_device,
                    source_record_id=event.source_record_id,
                    capture_method=event.capture_method,
                    quality_flags=None,
                    confidence=None,
                    coverage=None,
                    sensitivity=event.sensitivity,
                    consent_scope=event.consent_scope,
                    retention_policy_id=raw_policy.id,
                    expires_at=expires_at,
                    payload={
                        "operation_fingerprint": event.payload.get(
                            "operation_fingerprint"
                        ),
                        "source_text": source_text,
                        "media_path": media_path,
                        "warnings": (
                            warnings
                            if isinstance(warnings, list)
                            else []
                        ),
                        "item_warnings": item_warnings,
                        "legacy_raw_texts": raw_texts,
                    },
                    raw_object_id=raw_object_id,
                    derived_from={
                        "interaction_id": event.source_record_id
                    },
                )
            )
            raw_record_ids.add(event.source_record_id)
        event.payload = _strip_legacy_raw_fields(event.payload)
        event.quality_flags = {
            "warning_count": (
                event.quality_flags.get("warning_count", 0)
                if isinstance(event.quality_flags, dict)
                else 0
            )
        }
    durable_legacy_events = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type.in_(
                (
                    "nutrition.intake-outcome.v1",
                    "nutrition.decision-request.v1",
                    "nutrition.decision.v1",
                )
            )
        )
    )
    for event in durable_legacy_events:
        note = event.payload.get("note")
        if (
            event.event_type == "nutrition.intake-outcome.v1"
            and isinstance(note, str)
            and note
        ):
            raw_source_record_id = event.source_record_id
            existing_raw = session.scalar(
                select(WellnessEvent.id).where(
                    WellnessEvent.source_provider
                    == "nutrition-outcome-raw",
                    WellnessEvent.source_record_id
                    == raw_source_record_id,
                )
            )
            raw_expires_at = (
                None
                if not raw_policy.enabled
                or raw_policy.retention_days is None
                else _as_utc(event.recorded_at)
                + timedelta(days=raw_policy.retention_days)
            )
            if existing_raw is None and (
                raw_expires_at is None
                or raw_expires_at > _as_utc(current)
            ):
                session.add(
                    WellnessEvent(
                        event_type="nutrition.outcome-raw.v1",
                        schema_version=1,
                        observed_at=event.recorded_at,
                        recorded_at=event.recorded_at,
                        timezone=event.timezone,
                        source_provider="nutrition-outcome-raw",
                        source_device=event.source_device,
                        source_record_id=raw_source_record_id,
                        capture_method="manual",
                        quality_flags=None,
                        confidence=None,
                        sensitivity=event.sensitivity,
                        consent_scope=event.consent_scope,
                        retention_policy_id=raw_policy.id,
                        expires_at=raw_expires_at,
                        payload={
                            "operation_fingerprint": event.payload.get(
                                "operation_fingerprint"
                            ),
                            "note": note,
                        },
                        derived_from={
                            "outcome_id": raw_source_record_id
                        },
                    )
                )
        event.payload = _strip_legacy_raw_fields(event.payload)
        if isinstance(event.quality_flags, dict):
            event.quality_flags = _strip_legacy_raw_fields(
                event.quality_flags
            )


def run_storage_maintenance(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    maintenance_budget: MaintenanceBudget | None = None,
) -> StorageMaintenanceReport:
    with activity_write_lock():
        if session.new or session.dirty or session.deleted:
            raise RuntimeError(
                "storage maintenance requires a session without pending changes"
            )
        if session_holds_write_plane(session):
            raise RuntimeError(
                "storage maintenance requires the caller to commit or roll back "
                "its existing write transaction"
            )
        if session.in_transaction():
            # A read starts a SQLAlchemy transaction too. It is safe to end
            # only when no pending state or write-plane marker exists, and
            # doing so ensures maintenance reads begin after the global fence.
            session.rollback()
        with _guarded_maintenance_session(session) as guarded_session:
            session = guarded_session
            maintenance_budget = (
                maintenance_budget
                or _storage_maintenance_budget(settings)
            )
            budget_status = _MaintenanceBudgetStatus()
            try:
                plan = _prepare_storage_maintenance(
                    session,
                    settings,
                    dry_run=dry_run,
                    now=now,
                    maintenance_budget=maintenance_budget,
                    budget_status=budget_status,
                )
                # The database must make payloads unreachable before bytes are
                # removed. A failed or ambiguous commit therefore leaves every
                # file intact; a later run retries any committed purge row.
                session.commit()
            except BaseException:
                _rollback_maintenance_session(session)
                raise

            if plan.dry_run:
                return _maintenance_report(
                    plan,
                    budget_status=budget_status,
                )

            (
                reclaimed,
                files_deleted,
                cleanup_errors,
                completed_cleanup,
                manual_review_cleanup,
            ) = _cleanup_purged_files(
                settings,
                plan.file_cleanup,
                maintenance_budget=maintenance_budget,
                budget_status=budget_status,
            )
            errors = list(plan.precommit_errors + cleanup_errors)
            cleanup_completed_at = _now()
            acknowledged_cleanup: list[_PendingFileCleanup] = []
            manual_review_object_ids: list[str] = []
            for manual_review in manual_review_cleanup:
                candidate = manual_review.candidate
                obj = session.get(StorageObject, candidate.object_id)
                if obj is None:
                    errors.append(
                        "storage object disappeared before manual-review "
                        f"state could be recorded: {candidate.object_id}"
                    )
                    continue
                try:
                    stored_identity = _normalize_cleanup_identity(
                        obj.file_cleanup_identity
                    )
                except ValueError as exc:
                    errors.append(
                        "storage object cleanup identity became invalid before "
                        f"manual-review state: {candidate.object_id}: {exc}"
                    )
                    continue
                if (
                    obj.purged_at is None
                    or obj.file_cleanup_completed_at is not None
                    or stored_identity != candidate.identity
                ):
                    errors.append(
                        "storage object generation changed before manual-review "
                        f"state could be recorded: {candidate.object_id}"
                    )
                    continue
                stored_identity["manual_review_required"] = (
                    manual_review.reason
                )
                obj.file_cleanup_identity = stored_identity
                manual_review_object_ids.append(str(candidate.object_id))
            for candidate in completed_cleanup:
                obj = session.get(StorageObject, candidate.object_id)
                if obj is None:
                    errors.append(
                        "storage object disappeared before file cleanup "
                        f"could be acknowledged: {candidate.object_id}"
                    )
                    continue
                try:
                    stored_identity = _normalize_cleanup_identity(
                        obj.file_cleanup_identity
                    )
                except ValueError as exc:
                    errors.append(
                        "storage object cleanup identity became invalid before "
                        f"acknowledgement: {candidate.object_id}: {exc}"
                    )
                    continue
                if (
                    obj.purged_at is None
                    or obj.file_cleanup_completed_at is not None
                    or stored_identity != candidate.identity
                ):
                    errors.append(
                        "storage object generation changed before file cleanup "
                        f"could be acknowledged: {candidate.object_id}"
                    )
                    continue
                obj.file_cleanup_completed_at = cleanup_completed_at
                acknowledged_cleanup.append(candidate)
            try:
                session.flush()
                pending_cleanup = _count_pending_file_cleanup(session)
                _resolve_recovered_purge_jobs(
                    session,
                    current_job_id=plan.job_id,
                    completed_at=cleanup_completed_at,
                )
                job = session.get(PurgeJob, plan.job_id)
                if job is None:
                    raise RuntimeError(
                        "committed storage maintenance job could not be reloaded"
                    )
                job.finished_at = _now()
                job.status = (
                    "pending_file_cleanup"
                    if pending_cleanup
                    else (
                        "completed_with_errors"
                        if errors
                        else "completed"
                    )
                )
                job.bytes_reclaimed = reclaimed
                detail = dict(job.detail or {})
                detail.update(
                    {
                        "errors": list(errors),
                        "file_cleanup_candidates": len(plan.file_cleanup),
                        "file_cleanup_completed": len(
                            acknowledged_cleanup
                        ),
                        "file_cleanup_pending": (
                            pending_cleanup
                        ),
                        "file_cleanup_retries": sum(
                            candidate.retry
                            for candidate in plan.file_cleanup
                        ),
                        "file_cleanup_manual_review_object_ids": (
                            manual_review_object_ids
                        ),
                        "files_deleted": files_deleted,
                        "records_purged": plan.records_purged,
                    }
                )
                job.detail = detail
                session.commit()
            except BaseException:
                _rollback_maintenance_session(session)
                raise
            for candidate in acknowledged_cleanup:
                try:
                    _remove_cleanup_journal(
                        settings,
                        candidate.object_id,
                        maintenance_budget=maintenance_budget,
                    )
                except MaintenanceBudgetExceeded as exc:
                    budget_status.record(exc)
                    logger.warning(
                        "storage maintenance budget ended before completed "
                        "cleanup journal retirement for %s",
                        candidate.object_id,
                    )
                    break
                except (OSError, RuntimeError, ValueError):
                    logger.warning(
                        "could not retire completed storage cleanup journal "
                        "for %s",
                        candidate.object_id,
                        exc_info=True,
                    )
            return _maintenance_report(
                plan,
                bytes_reclaimed=reclaimed,
                files_deleted=files_deleted,
                file_cleanup_pending=pending_cleanup,
                errors=tuple(errors),
                budget_status=budget_status,
            )


def _resolve_recovered_purge_jobs(
    session: Session,
    *,
    current_job_id: uuid.UUID,
    completed_at: datetime,
) -> None:
    """Close crash-stranded jobs after their own files are acknowledged."""

    # Session factories disable autoflush. Make this run's acknowledgements
    # visible before deciding whether any purge still needs byte cleanup.
    session.flush()
    stranded = session.scalars(
        select(PurgeJob).where(
            PurgeJob.status == "pending_file_cleanup",
            PurgeJob.id != current_job_id,
        )
    )
    for job in stranded:
        detail = dict(job.detail or {})
        raw_ids = detail.get("file_cleanup_object_ids")
        object_ids: list[uuid.UUID] | None = None
        if isinstance(raw_ids, list):
            try:
                object_ids = [
                    uuid.UUID(value)
                    for value in raw_ids
                    if isinstance(value, str)
                ]
            except ValueError:
                object_ids = None
            if object_ids is not None and len(object_ids) != len(raw_ids):
                object_ids = None
        unresolved_query = select(StorageObject.id).where(
            StorageObject.purged_at.is_not(None),
            StorageObject.file_cleanup_completed_at.is_(None),
        )
        if object_ids is not None:
            if object_ids:
                unresolved_query = unresolved_query.where(
                    StorageObject.id.in_(object_ids)
                )
            else:
                unresolved_query = unresolved_query.where(
                    StorageObject.id.is_(None)
                )
        if session.scalar(unresolved_query.limit(1)) is not None:
            continue
        errors = detail.get("errors")
        job.status = (
            "completed_with_errors"
            if isinstance(errors, list) and errors
            else "completed"
        )
        job.finished_at = completed_at
        detail["file_cleanup_completed"] = detail.get(
            "file_cleanup_candidates",
            detail.get("file_cleanup_completed", 0),
        )
        detail["file_cleanup_pending"] = 0
        detail["file_cleanup_recovered_by_job_id"] = str(
            current_job_id
        )
        detail["file_cleanup_recovered_at"] = completed_at.isoformat()
        job.detail = detail


def _maintenance_report(
    plan: _StorageMaintenancePlan,
    *,
    bytes_reclaimed: int = 0,
    files_deleted: int = 0,
    file_cleanup_pending: int | None = None,
    errors: tuple[str, ...] | None = None,
    budget_status: _MaintenanceBudgetStatus | None = None,
) -> StorageMaintenanceReport:
    status = budget_status or _MaintenanceBudgetStatus(
        resource=plan.budget_resource,
        phase=plan.budget_phase,
    )
    return StorageMaintenanceReport(
        job_id=str(plan.job_id),
        dry_run=plan.dry_run,
        candidates=plan.candidates,
        records_purged=plan.records_purged,
        files_deleted=files_deleted,
        file_cleanup_pending=(
            plan.file_cleanup_pending
            if file_cleanup_pending is None
            else file_cleanup_pending
        ),
        # Compatibility alias: historically ``deleted`` meant the payload
        # file was physically removed, not merely tombstoned in the database.
        deleted=files_deleted,
        bytes_reclaimed=bytes_reclaimed,
        decision_candidates=plan.decision_candidates,
        decisions_deleted=(
            0 if plan.dry_run else plan.decision_candidates
        ),
        decision_receipt_candidates=plan.decision_receipt_candidates,
        decision_receipts_deleted=(
            0 if plan.dry_run else plan.decision_receipt_candidates
        ),
        budget_exhausted=status.resource is not None,
        budget_resource=status.resource,
        budget_phase=status.phase,
        errors=plan.precommit_errors if errors is None else errors,
    )


def _prepare_storage_maintenance(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    maintenance_budget: MaintenanceBudget | None = None,
    budget_status: _MaintenanceBudgetStatus | None = None,
) -> _StorageMaintenancePlan:
    maintenance_budget = maintenance_budget or _storage_maintenance_budget(
        settings
    )
    budget_status = budget_status or _MaintenanceBudgetStatus()
    current = now or _now()
    if not dry_run:
        lock_activity_write_plane(session)
        lock_trigger_events_for_retention(session)
    ensure_default_policies(session)
    decision_policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "decision"
        )
    )
    if decision_policy is not None and not dry_run:
        _recalculate_expiry(
            session,
            decision_policy,
            previous_retention_days=decision_policy.retention_days,
            now=_as_utc(current),
        )
    _migrate_legacy_nutrition_raw_captures(
        session,
        current=current,
        dry_run=dry_run,
    )
    job = PurgeJob(started_at=current, dry_run=dry_run, status="running")
    session.add(job)
    session.flush()
    if not dry_run:
        # Activity retention owns summary deletion and downstream baseline
        # refresh. Run it before the generic event purge so those scopes are
        # still available for deterministic repair.
        from healthmes.activity.maintenance import run_activity_maintenance

        run_activity_maintenance(session, now=current)
        calendar_policy = session.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.data_class == "calendar_mirror"
            )
        )
        if calendar_policy is not None:
            purge_expired_calendar_mirrors(
                session,
                cutoff=retention_cutoff(
                    session,
                    "calendar_mirror",
                    now=current,
                ),
            )
    decision_candidates = purge_expired_decision_records(
        session,
        now=current,
        dry_run=dry_run,
    )
    decision_receipt_candidates = purge_expired_decision_receipts(
        session,
        now=current,
        dry_run=dry_run,
    )

    records_purged = 0
    errors: list[str] = []
    file_cleanup: list[_PendingFileCleanup] = []
    discovery_truncated = False
    budget_blocked = False

    if not dry_run:
        try:
            errors.extend(
                _reconcile_completed_cleanup_journals(
                    session,
                    settings,
                    maintenance_budget=maintenance_budget,
                )
            )
        except MaintenanceBudgetExceeded as exc:
            budget_status.record(exc)
            errors.append(_budget_error_text(exc))
            budget_blocked = True

    retry_filter = (
        StorageObject.purged_at.is_not(None),
        StorageObject.file_cleanup_completed_at.is_(None),
    )
    retry_count = int(
        session.scalar(
            select(func.count())
            .select_from(StorageObject)
            .where(*retry_filter)
        )
        or 0
    )
    retry_candidates = list(
        session.scalars(
            select(StorageObject)
            .where(*retry_filter)
            .order_by(StorageObject.updated_at, StorageObject.id)
            .limit(_MAINTENANCE_RETRY_OBJECT_LIMIT)
        )
    )
    if retry_count > len(retry_candidates):
        errors.append(
            "pending storage file cleanup processing was truncated at "
            f"{_MAINTENANCE_RETRY_OBJECT_LIMIT} entries"
        )
    for obj in retry_candidates:
        if dry_run or budget_blocked:
            break
        try:
            maintenance_budget.checkpoint(
                phase="pending storage cleanup identity preparation"
            )
            _validate_cleanup_relative_path(obj.relative_path)
            identity_changed = False
            if obj.file_cleanup_identity is None:
                identity = _capture_pre_identity_cleanup(
                    settings,
                    obj,
                    maintenance_budget=maintenance_budget,
                )
                identity_changed = True
            else:
                identity = _normalize_cleanup_identity(
                    obj.file_cleanup_identity
                )
                if (
                    identity.get("version")
                    == _LEGACY_FILE_CLEANUP_IDENTITY_VERSION
                ):
                    identity = _upgrade_legacy_cleanup_identity(
                        settings,
                        obj,
                        identity,
                        maintenance_budget=maintenance_budget,
                    )
                    identity_changed = True
        except MaintenanceBudgetExceeded as exc:
            budget_status.record(exc)
            errors.append(
                f"{obj.relative_path}: {_budget_error_text(exc)}"
            )
            budget_blocked = True
            break
        except (OSError, ValueError) as exc:
            errors.append(f"{obj.relative_path}: {exc}")
            continue
        if identity_changed:
            obj.file_cleanup_identity = identity
        obj.updated_at = current
        file_cleanup.append(
            _PendingFileCleanup(
                object_id=obj.id,
                relative_path=obj.relative_path,
                size_bytes=obj.size_bytes,
                identity=identity,
                retry=True,
            )
        )

    if not budget_blocked:
        try:
            discovery_truncated = _discover_unindexed(
                session,
                settings,
                max_entries=_MAINTENANCE_DISCOVERY_ENTRY_LIMIT,
                deadline=(
                    time.monotonic()
                    + _MAINTENANCE_DISCOVERY_MAX_SECONDS
                ),
                maintenance_budget=maintenance_budget,
            )
        except MaintenanceBudgetExceeded as exc:
            budget_status.record(exc)
            errors.append(_budget_error_text(exc))
            discovery_truncated = True
            budget_blocked = True
    if discovery_truncated:
        errors.append(
            "unindexed storage discovery reached its bounded maintenance slice"
        )

    candidate_filter = (
        StorageObject.purged_at.is_(None),
        StorageObject.safe_to_purge.is_(True),
        StorageObject.expires_at.is_not(None),
        StorageObject.expires_at <= current,
    )
    candidate_count = int(
        session.scalar(
            select(func.count())
            .select_from(StorageObject)
            .where(*candidate_filter)
        )
        or 0
    )
    candidates = list(
        session.scalars(
            select(StorageObject)
            .where(*candidate_filter)
            .order_by(StorageObject.updated_at, StorageObject.id)
            .limit(_MAINTENANCE_NEW_OBJECT_LIMIT)
        )
    )
    if candidate_count > len(candidates):
        errors.append(
            "expired storage object processing was truncated at "
            f"{_MAINTENANCE_NEW_OBJECT_LIMIT} entries"
        )
    for obj in candidates:
        try:
            _validate_cleanup_relative_path(obj.relative_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if dry_run or budget_blocked:
            continue
        try:
            maintenance_budget.checkpoint(
                phase="new storage tombstone identity preparation"
            )
            identity = _capture_cleanup_identity(
                settings,
                obj.relative_path,
                expected_size=obj.size_bytes,
                expected_sha256=obj.sha256,
                maintenance_budget=maintenance_budget,
            )
            identity = _normalize_cleanup_identity(identity)
        except MaintenanceBudgetExceeded as exc:
            budget_status.record(exc)
            errors.append(
                f"{obj.relative_path}: {_budget_error_text(exc)}"
            )
            budget_blocked = True
            break
        except (OSError, ValueError) as exc:
            errors.append(f"{obj.relative_path}: {exc}")
            continue
        if obj.data_class in {"media", "nutrition_media"}:
            food_rows = session.scalars(
                select(FoodLog).where(FoodLog.media_path == obj.relative_path)
            )
            for row in food_rows:
                row.media_path = None
            medical_rows = session.scalars(
                select(MedicalRecord).where(
                    MedicalRecord.media_path == obj.relative_path
                )
            )
            for row in medical_rows:
                row.media_path = None
        if obj.data_class == "raw_payload":
            raw = session.scalar(
                select(RawIngestEvent).where(
                    RawIngestEvent.path == obj.relative_path
                )
            )
            if raw is not None:
                session.delete(raw)
        obj.purged_at = current
        obj.file_cleanup_identity = identity
        obj.updated_at = current
        records_purged += 1
        file_cleanup.append(
            _PendingFileCleanup(
                object_id=obj.id,
                relative_path=obj.relative_path,
                size_bytes=obj.size_bytes,
                identity=identity,
                retry=False,
            )
        )
    if not dry_run:
        session.execute(
            delete(WellnessEvent).where(
                WellnessEvent.expires_at.is_not(None),
                WellnessEvent.expires_at <= current,
                WellnessEvent.event_type.not_like("activity.%"),
            )
        )
    if dry_run:
        job.finished_at = _now()
        job.status = "completed_with_errors" if errors else "completed"
    else:
        job.status = "pending_file_cleanup"
    session.flush()
    pending_file_cleanup = _count_pending_file_cleanup(session)
    pending_object_ids = {
        obj.id for obj in retry_candidates
    } | {
        candidate.object_id for candidate in file_cleanup
    }
    job.candidates = candidate_count
    # PurgeJob.deleted is a legacy database column whose persisted meaning is
    # the number of storage records made unreachable in this transaction.
    job.deleted = records_purged
    job.bytes_reclaimed = 0
    job.detail = {
        "errors": errors,
        "decision_candidates": decision_candidates,
        "decisions_deleted": (
            0 if dry_run else decision_candidates
        ),
        "decision_receipt_candidates": (
            decision_receipt_candidates
        ),
        "decision_receipts_deleted": (
            0 if dry_run else decision_receipt_candidates
        ),
        "file_cleanup_candidates": len(file_cleanup),
        "file_cleanup_completed": 0,
        "file_cleanup_pending": pending_file_cleanup,
        "file_cleanup_retries": retry_count,
        "files_deleted": 0,
        "records_purged": records_purged,
        "file_cleanup_object_ids": [
            str(object_id)
            for object_id in sorted(pending_object_ids, key=str)
        ],
    }
    return _StorageMaintenancePlan(
        job_id=job.id,
        dry_run=dry_run,
        candidates=candidate_count,
        records_purged=records_purged,
        file_cleanup_pending=pending_file_cleanup,
        decision_candidates=decision_candidates,
        decision_receipt_candidates=decision_receipt_candidates,
        budget_resource=budget_status.resource,
        budget_phase=budget_status.phase,
        precommit_errors=tuple(errors),
        file_cleanup=tuple(file_cleanup),
    )
