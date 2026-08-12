"""Scheduled ActivityWatch import using the existing safe adapter boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session, sessionmaker

from healthmes.activity.activitywatch import (
    ActivityWatchClient,
    ActivityWatchError,
    StaleActivityWatchImportError,
    import_activitywatch,
)
from healthmes.activity.contracts import (
    ActivityBatchOut,
    ActivityPlatform,
    ActivityWatchImportRequest,
)
from healthmes.activity.repository import get_control_payload
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    StaleCollectionRevisionError,
)
from healthmes.config import Settings, resolve_timezone
from healthmes.store.session import session_scope

logger = logging.getLogger(__name__)

ActivityWatchClientFactory = Callable[[], ActivityWatchClient]
NowProvider = Callable[[], datetime]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("ActivityWatch scheduler time must be timezone-aware")
    return value.astimezone(UTC)


def _has_activitywatch_cursor(state: dict[str, object]) -> bool:
    cursors = state.get("cursors")
    return isinstance(cursors, dict) and any(
        isinstance(key, str) and key.startswith("activitywatch:")
        for key in cursors
    )


def build_activitywatch_job(
    settings: Settings,
    *,
    client_factory: ActivityWatchClientFactory | None = None,
    session_factory: sessionmaker[Session] | None = None,
    now_provider: NowProvider | None = None,
) -> Callable[[], ActivityBatchOut | None] | None:
    """Build a contained periodic import, or ``None`` when it is disabled.

    The first successful run is bounded by ``activitywatch_window_minutes``.
    Once the adapter has persisted a cursor, subsequent runs omit an explicit
    range so ``import_activitywatch`` owns overlap and cursor progression.
    """
    if not settings.activitywatch_enabled:
        return None

    platform = ActivityPlatform(settings.activitywatch_platform)
    timezone = settings.activitywatch_timezone or str(resolve_timezone(settings))

    def run_activitywatch_import() -> ActivityBatchOut | None:
        if not settings.activitywatch_enabled:
            logger.info("ActivityWatch scheduled import disabled; skipping.")
            return None
        try:
            current = _as_utc(
                now_provider() if now_provider is not None else datetime.now(UTC)
            )
            with session_scope(session_factory) as session:
                state = get_control_payload(
                    session,
                    settings.activitywatch_device_id,
                    platform=platform,
                )
                request_updates: dict[str, datetime] = {}
                if not _has_activitywatch_cursor(state):
                    request_updates = {
                        "start_at": current
                        - timedelta(minutes=settings.activitywatch_window_minutes),
                        "end_at": current,
                    }
                request = ActivityWatchImportRequest(
                    device_id=settings.activitywatch_device_id,
                    platform=platform,
                    timezone=timezone,
                    base_url=settings.activitywatch_base_url,
                    window_bucket_id=settings.activitywatch_window_bucket_id,
                    afk_bucket_id=settings.activitywatch_afk_bucket_id,
                    **request_updates,
                )
                client = (
                    client_factory()
                    if client_factory is not None
                    else ActivityWatchClient(
                        settings.activitywatch_base_url,
                        timeout=settings.activitywatch_timeout_seconds,
                    )
                )
                result = import_activitywatch(
                    session,
                    request,
                    client=client,
                    now=current,
                )
                return ActivityBatchOut.model_validate(result.response)
        except ActivityCollectionBlockedError as exc:
            logger.info(
                "ActivityWatch scheduled import skipped for %s: %s.",
                settings.activitywatch_device_id,
                exc.reason,
            )
        except (StaleActivityWatchImportError, StaleCollectionRevisionError) as exc:
            logger.info(
                "ActivityWatch scheduled import superseded for %s: %s.",
                settings.activitywatch_device_id,
                exc,
            )
        except (ActivityWatchError, httpx.HTTPError) as exc:
            logger.warning(
                "ActivityWatch scheduled import failed for %s; next interval "
                "will retry: %s",
                settings.activitywatch_device_id,
                exc,
            )
        except Exception:
            logger.exception(
                "ActivityWatch scheduled import failed for %s; next interval will retry.",
                settings.activitywatch_device_id,
            )
        return None

    return run_activitywatch_import
