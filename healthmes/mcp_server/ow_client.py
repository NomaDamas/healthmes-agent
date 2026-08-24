"""Read-only httpx client for the open-wearables REST API (v1).

Every path, query parameter, and header below is grounded in vendor code —
never invent endpoints:

- Auth header ``X-Open-Wearables-API-Key`` and the per-request
  ``httpx.AsyncClient`` pattern follow
  ``vendor/open-wearables/mcp/app/services/api_client.py``.
- Paths/params mirror ``vendor/open-wearables/backend/app/api/routes/v1/``:
  ``users.py``, ``health_scores.py``, ``summaries.py``, ``timeseries.py``,
  ``events.py``, ``data_sources.py``, ``meta.py``, ``oauth.py``, and
  ``vendor_workouts.py``.
- Date-ish params accept ISO-8601 datetimes, date-only strings (normalized to
  midnight UTC), or unix seconds (``app/utils/dates.py::parse_query_datetime``).

Base URL and API key come from :class:`healthmes.config.Settings`
(``HEALTHMES_OW_BASE_URL`` / ``HEALTHMES_OW_API_KEY``); never hardcode hosts.
The optional ``transport`` argument exists so tests can inject
``httpx.MockTransport`` — no network needed.
"""

import inspect
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from healthmes.config import Settings

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Page caps for the collect_* helpers so a pathological backend response can
# never turn into an unbounded fetch loop.
MAX_PAGES = 10
# Timeseries pages are denser: one day of 1-minute samples is 1440 rows,
# i.e. 15 pages at the route's max limit of 100 — give the collector headroom.
TIMESERIES_MAX_PAGES = 20

# Per-route maximum ``limit`` values (FastAPI Query bounds in the vendor routes).
HEALTH_SCORES_MAX_LIMIT = 1000  # health_scores.py: Query(ge=1, le=1000)
SUMMARIES_MAX_LIMIT = 100  # summaries.py sleep/recovery: Query(ge=1, le=100)
ACTIVITY_MAX_LIMIT = 400  # summaries.py activity: Query(ge=1, le=400)
EVENTS_MAX_LIMIT = 100  # events.py: Query(ge=1, le=100)
TIMESERIES_MAX_LIMIT = 100  # timeseries.py: Query(ge=1, le=100)
VENDOR_WORKOUTS_MAX_LIMIT = 100  # vendor_workouts.py: Query(le=100)
MAX_RESPONSE_BYTES = 512_000

Resolution = Literal["raw", "1min", "5min", "15min", "1hour"]


class OWClientError(Exception):
    """Base error for open-wearables client failures."""


class OWConfigurationError(OWClientError):
    """The client is missing required configuration (e.g. empty API key)."""


class OWAuthError(OWClientError):
    """The backend rejected the API key (HTTP 401)."""


class OWNotFoundError(OWClientError):
    """The requested resource does not exist (HTTP 404)."""


class OWPayloadError(OWClientError):
    """The backend returned a successful response with an invalid body."""


@dataclass(frozen=True, slots=True)
class VendorWorkoutCollection:
    """Provider workout rows plus distinct completeness signals."""

    rows: tuple[dict[str, Any], ...]
    truncated: bool = False
    completeness_unverified: bool = False


def _require_int_range(
    name: str,
    value: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    """Apply only the integer bounds declared by the matching vendor route."""
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be less than or equal to {maximum}")


def _window_epoch_seconds(value: str) -> int:
    """Parse a required vendor-workout window bound as UTC epoch seconds."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "vendor workout window bounds must be ISO-8601 or unix seconds"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


class OWClient:
    """Async client for the open-wearables backend REST API (read-only consumer)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> "OWClient":
        """Build a client from HealthMes settings (ow_base_url + ow_api_key)."""
        return cls(
            base_url=settings.ow_base_url,
            api_key=settings.ow_api_key.get_secret_value(),
        )

    @property
    def headers(self) -> dict[str, str]:
        """Request headers (same contract as the vendor MCP api_client)."""
        return {
            "X-Open-Wearables-API-Key": self._api_key,
            "Content-Type": "application/json",
        }

    def _ensure_configured(self) -> None:
        if not self._api_key:
            raise OWConfigurationError(
                "open-wearables API key is not configured; set HEALTHMES_OW_API_KEY"
            )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``{base_url}{path}`` and return the parsed JSON body."""
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        logger.debug("GET open-wearables resource")

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers=self.headers,
                    params=params,
                ) as response:
                    if response.status_code == 401:
                        raise OWAuthError(
                            "open-wearables rejected the API key (401)"
                        )
                    if response.status_code == 404:
                        raise OWNotFoundError(
                            "open-wearables resource not found (404)"
                        )
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise OWClientError(
                            "open-wearables request failed "
                            f"({response.status_code})"
                        ) from None

                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_bytes = int(content_length)
                        except ValueError:
                            raise OWClientError(
                                "open-wearables returned an invalid "
                                "content length"
                            ) from None
                        if (
                            declared_bytes < 0
                            or declared_bytes > MAX_RESPONSE_BYTES
                        ):
                            raise OWClientError(
                                "open-wearables response exceeded the "
                                "byte limit"
                            )

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise OWClientError(
                                "open-wearables response exceeded the "
                                "byte limit"
                            )
                        body.extend(chunk)
        except httpx.HTTPError:
            raise OWClientError("open-wearables request failed (transport)") from None

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OWPayloadError(
                "open-wearables returned invalid JSON"
            ) from None
        return payload

    # ------------------------------------------------------------------
    # Users (routes/v1/users.py — OldPaginatedResponse: items/total/page/limit)
    # ------------------------------------------------------------------

    async def list_users(
        self, *, search: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """GET /api/v1/users — users visible to the configured API key."""
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        return await self._get("/api/v1/users", params=params)

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """GET /api/v1/users/{user_id} — one Open Wearables user."""
        return await self._get(f"/api/v1/users/{user_id}")

    async def get_connections(self, user_id: str) -> list[dict[str, Any]]:
        payload = await self._get(f"/api/v1/users/{user_id}/connections")
        if not isinstance(payload, list) or any(
            not isinstance(row, Mapping) for row in payload
        ):
            raise OWClientError("open-wearables returned an invalid connections response")
        return [dict(row) for row in payload]

    async def get_user_data_sources(self, user_id: str) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/data-sources."""
        payload = await self._get(f"/api/v1/users/{user_id}/data-sources")
        if not isinstance(payload, Mapping):
            raise OWClientError(
                "open-wearables returned an invalid data sources response"
            )
        items = payload.get("items")
        total = payload.get("total")
        if (
            not isinstance(items, list)
            or any(not isinstance(row, Mapping) for row in items)
            or type(total) is not int
            or total < 0
        ):
            raise OWClientError(
                "open-wearables returned an invalid data sources response"
            )
        return {
            "items": [dict(row) for row in items],
            "total": total,
        }

    # ------------------------------------------------------------------
    # Metadata (routes/v1/meta.py and routes/v1/oauth.py)
    # ------------------------------------------------------------------

    async def get_provider_coverage(self) -> dict[str, Any]:
        """GET /api/v1/meta/coverage — static provider data coverage matrix."""
        return await self._get("/api/v1/meta/coverage")

    async def get_configured_providers(
        self,
        *,
        enabled_only: bool = False,
        cloud_only: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/providers — configured provider metadata."""
        payload = await self._get(
            "/api/v1/providers",
            params={
                "enabled_only": enabled_only,
                "cloud_only": cloud_only,
            },
        )
        if not isinstance(payload, list):
            raise OWClientError("open-wearables returned an invalid providers response")
        return payload

    # ------------------------------------------------------------------
    # Health scores (routes/v1/health_scores.py)
    # ------------------------------------------------------------------

    async def get_health_scores(
        self,
        user_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        category: str | None = None,
        provider: str | None = None,
        limit: int = HEALTH_SCORES_MAX_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/health-scores (offset pagination)."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        if category is not None:
            params["category"] = category
        if provider is not None:
            params["provider"] = provider
        return await self._get(f"/api/v1/users/{user_id}/health-scores", params=params)

    async def collect_health_scores(
        self,
        user_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        category: str | None = None,
        provider: str | None = None,
    ) -> list[dict[str, Any]]:
        """All health-score rows in a window, following offset pagination."""
        rows, _truncated = await self.collect_health_scores_tracked(
            user_id,
            start_date=start_date,
            end_date=end_date,
            category=category,
            provider=provider,
        )
        return rows

    async def collect_health_scores_tracked(
        self,
        user_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        category: str | None = None,
        provider: str | None = None,
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Like :meth:`collect_health_scores`, plus a truncation flag."""
        rows: list[dict[str, Any]] = []
        offset = 0
        has_more = False
        for _ in range(max_pages):
            payload = await self.get_health_scores(
                user_id,
                start_date=start_date,
                end_date=end_date,
                category=category,
                provider=provider,
                offset=offset,
            )
            data = payload.get("data", [])
            rows.extend(data)
            pagination = payload.get("pagination", {})
            has_more = bool(pagination.get("has_more"))
            if not has_more or not data:
                return rows, False
            offset += len(data)
        if has_more:
            logger.warning(
                "offset pagination stopped at max_pages=%d with more data available",
                max_pages,
            )
        return rows, has_more

    # ------------------------------------------------------------------
    # Summaries (routes/v1/summaries.py — cursor pagination)
    # ------------------------------------------------------------------

    async def get_body_summary(
        self,
        user_id: str,
        *,
        average_period: int = 7,
        latest_window_hours: int = 4,
    ) -> dict[str, Any] | None:
        """GET /api/v1/users/{user_id}/summaries/body."""
        _require_int_range("average_period", average_period, minimum=1, maximum=7)
        _require_int_range(
            "latest_window_hours",
            latest_window_hours,
            minimum=1,
            maximum=24,
        )
        payload = await self._get(
            f"/api/v1/users/{user_id}/summaries/body",
            params={
                "average_period": average_period,
                "latest_window_hours": latest_window_hours,
            },
        )
        if payload is not None and not isinstance(payload, dict):
            raise OWClientError(
                "open-wearables returned an invalid body summary response"
            )
        return payload

    async def get_data_summary(
        self,
        user_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/summaries/data."""
        params: dict[str, Any] = {}
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        return await self._get(
            f"/api/v1/users/{user_id}/summaries/data",
            params=params,
        )

    async def get_sleep_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        cursor: str | None = None,
        limit: int = SUMMARIES_MAX_LIMIT,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/summaries/sleep — daily sleep metrics."""
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get(f"/api/v1/users/{user_id}/summaries/sleep", params=params)

    async def get_recovery_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        cursor: str | None = None,
        limit: int = SUMMARIES_MAX_LIMIT,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/summaries/recovery — daily recovery metrics."""
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get(f"/api/v1/users/{user_id}/summaries/recovery", params=params)

    async def get_activity_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        cursor: str | None = None,
        limit: int = SUMMARIES_MAX_LIMIT,
        sort_order: Literal["asc", "desc"] | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/summaries/activity — daily activity metrics."""
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if sort_order is not None:
            params["sort_order"] = sort_order
        return await self._get(f"/api/v1/users/{user_id}/summaries/activity", params=params)

    async def collect_sleep_summaries(
        self, user_id: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """All daily sleep summaries in a window, following cursor pagination."""
        rows, _truncated = await self.collect_sleep_summaries_tracked(
            user_id, start_date, end_date
        )
        return rows

    async def collect_sleep_summaries_tracked(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Like :meth:`collect_sleep_summaries`, plus a truncation flag."""
        return await self._collect_cursor(
            lambda cursor: self.get_sleep_summaries(
                user_id, start_date, end_date, cursor=cursor
            ),
            max_pages=max_pages,
        )

    async def collect_recovery_summaries(
        self, user_id: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """All daily recovery summaries in a window, following cursor pagination."""
        rows, _truncated = await self.collect_recovery_summaries_tracked(
            user_id, start_date, end_date
        )
        return rows

    async def collect_recovery_summaries_tracked(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Like :meth:`collect_recovery_summaries`, plus a truncation flag."""
        return await self._collect_cursor(
            lambda cursor: self.get_recovery_summaries(
                user_id, start_date, end_date, cursor=cursor
            ),
            max_pages=max_pages,
        )

    # ------------------------------------------------------------------
    # Events (routes/v1/events.py — cursor pagination)
    # ------------------------------------------------------------------

    async def get_workouts(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        record_type: str | None = None,
        cursor: str | None = None,
        limit: int = EVENTS_MAX_LIMIT,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/events/workouts — workout sessions."""
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "limit": limit}
        if record_type is not None:
            params["record_type"] = record_type
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get(f"/api/v1/users/{user_id}/events/workouts", params=params)

    async def collect_workouts(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        record_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """All workouts in a window, following cursor pagination."""
        rows, _truncated = await self.collect_workouts_tracked(
            user_id, start_date, end_date, record_type=record_type
        )
        return rows

    async def collect_workouts_tracked(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        record_type: str | None = None,
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Like :meth:`collect_workouts`, plus a truncation flag."""
        return await self._collect_cursor(
            lambda cursor: self.get_workouts(
                user_id, start_date, end_date, record_type=record_type, cursor=cursor
            ),
            max_pages=max_pages,
        )

    async def get_menstrual_cycles(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        cursor: str | None = None,
        limit: int = EVENTS_MAX_LIMIT,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/events/menstrual-cycles — per-cycle records.

        Both date params are required by the vendor route (events.py::
        list_menstrual_cycles); server-side only ``start_date`` filters (cycles
        can end in the future, so the service drops the end filter). Rows are
        ``MenstrualCycleRecord`` shapes: start/end times plus current_phase_type,
        day_in_cycle, cycle_length, period_length, pregnancy_snapshot, ...
        """
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get(f"/api/v1/users/{user_id}/events/menstrual-cycles", params=params)

    async def collect_menstrual_cycles(
        self, user_id: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """All menstrual-cycle records in a window, following cursor pagination."""
        rows, _truncated = await self.collect_menstrual_cycles_tracked(
            user_id, start_date, end_date
        )
        return rows

    async def collect_menstrual_cycles_tracked(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Like :meth:`collect_menstrual_cycles`, plus a truncation flag."""
        return await self._collect_cursor(
            lambda cursor: self.get_menstrual_cycles(
                user_id, start_date, end_date, cursor=cursor
            ),
            max_pages=max_pages,
        )

    async def get_sleep_sessions(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        cursor: str | None = None,
        limit: int = EVENTS_MAX_LIMIT,
        filter_by_priority: bool | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/events/sleep — sleep sessions (incl. naps)."""
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if filter_by_priority is not None:
            params["filter_by_priority"] = filter_by_priority
        return await self._get(f"/api/v1/users/{user_id}/events/sleep", params=params)

    async def collect_sleep_sessions(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        rows, _truncated = await self.collect_sleep_sessions_tracked(
            user_id,
            start_date,
            end_date,
        )
        return rows

    async def collect_sleep_sessions_tracked(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        *,
        filter_by_priority: bool = True,
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """All sleep sessions in a window, plus page-cap truncation state."""
        return await self._collect_cursor(
            lambda cursor: self.get_sleep_sessions(
                user_id,
                start_date,
                end_date,
                cursor=cursor,
                filter_by_priority=filter_by_priority,
            ),
            max_pages=max_pages,
        )

    # ------------------------------------------------------------------
    # Provider workouts (routes/v1/vendor_workouts.py)
    # ------------------------------------------------------------------

    async def get_vendor_workouts(
        self,
        provider: str,
        user_id: str,
        *,
        since: int = 0,
        limit: int = 50,
        offset: int = 0,
        filter_by_modification_time: bool = True,
        samples: bool = False,
        zones: bool = False,
        route: bool = False,
        summary_start_time: str | None = None,
        summary_end_time: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """GET provider-native workouts with the vendor route's exact options."""
        _require_int_range(
            "limit",
            limit,
            maximum=VENDOR_WORKOUTS_MAX_LIMIT,
        )
        normalized_provider = provider.strip().lower()
        if not normalized_provider:
            raise ValueError("vendor workout provider must not be empty")

        params: dict[str, Any] = {}
        if normalized_provider == "suunto":
            params.update(
                {
                    "since": since,
                    "limit": limit,
                    "offset": offset,
                    "filter_by_modification_time": filter_by_modification_time,
                }
            )
        elif normalized_provider == "polar":
            params.update(
                {
                    "samples": samples,
                    "zones": zones,
                    "route": route,
                }
            )
        elif normalized_provider == "garmin":
            if summary_start_time is not None:
                params["summary_start_time"] = summary_start_time
            if summary_end_time is not None:
                params["summary_end_time"] = summary_end_time
        payload = await self._get(
            f"/api/v1/providers/{normalized_provider}/users/{user_id}/workouts",
            params=params or None,
        )
        if not isinstance(payload, (dict, list)):
            raise OWClientError(
                "open-wearables returned an invalid vendor workouts response"
            )
        return payload

    async def collect_vendor_workouts_tracked(
        self,
        provider: str,
        user_id: str,
        start_time: str,
        end_time: str,
        *,
        samples: bool = False,
        zones: bool = False,
        route: bool = False,
        max_pages: int = MAX_PAGES,
    ) -> VendorWorkoutCollection:
        """Return provider-native workout rows and completeness metadata.

        The vendored route exposes different window controls by provider:
        Garmin accepts both ISO/unix bounds, Suunto accepts only a unix
        ``since`` value and offset pagination, and other providers expose no
        common window parameters. Callers must still enforce the requested
        interval against each returned row. Garmin silently chunks windows
        longer than 24 hours upstream, so those results are marked as having
        unverified completeness without falsely reporting a HealthMes page
        limit.
        """
        start_epoch = _window_epoch_seconds(start_time)
        end_epoch = _window_epoch_seconds(end_time)
        if end_epoch <= start_epoch:
            raise ValueError("vendor workout end_time must be after start_time")

        normalized_provider = provider.strip().lower()
        if not normalized_provider:
            raise ValueError("vendor workout provider must not be empty")

        if normalized_provider == "suunto":
            rows: list[dict[str, Any]] = []
            offset = 0
            for _ in range(max_pages):
                payload = await self.get_vendor_workouts(
                    normalized_provider,
                    user_id,
                    since=start_epoch * 1_000,
                    limit=VENDOR_WORKOUTS_MAX_LIMIT,
                    offset=offset,
                )
                page = self._vendor_workout_rows(
                    payload,
                    provider=normalized_provider,
                )
                rows.extend(page)
                if len(page) < VENDOR_WORKOUTS_MAX_LIMIT:
                    return VendorWorkoutCollection(rows=tuple(rows))
                offset += len(page)
            return VendorWorkoutCollection(
                rows=tuple(rows),
                truncated=bool(rows),
            )

        payload = await self.get_vendor_workouts(
            normalized_provider,
            user_id,
            limit=VENDOR_WORKOUTS_MAX_LIMIT,
            samples=samples if normalized_provider == "polar" else False,
            zones=zones if normalized_provider == "polar" else False,
            route=route if normalized_provider == "polar" else False,
            summary_start_time=(
                start_time if normalized_provider == "garmin" else None
            ),
            summary_end_time=(
                end_time if normalized_provider == "garmin" else None
            ),
        )
        rows = self._vendor_workout_rows(
            payload,
            provider=normalized_provider,
        )
        continuation = self._vendor_workout_continuation(payload)
        garmin_chunk_completeness_unverified = (
            normalized_provider == "garmin"
            and end_epoch - start_epoch > 24 * 60 * 60
        )
        return VendorWorkoutCollection(
            rows=tuple(rows),
            truncated=(
                continuation
                or len(rows) >= VENDOR_WORKOUTS_MAX_LIMIT
            ),
            completeness_unverified=(
                garmin_chunk_completeness_unverified
            ),
        )

    async def get_vendor_workout_detail(
        self,
        provider: str,
        user_id: str,
        workout_id: str,
        *,
        samples: bool = False,
        zones: bool = False,
        route: bool = False,
    ) -> dict[str, Any]:
        """GET one provider-native workout detail record.

        For Suunto, ``workout_id`` is the opaque ``workoutKey`` returned by
        its workout list API, not the numeric ``workoutId``.
        """
        normalized_provider = provider.strip().lower()
        if not normalized_provider:
            raise ValueError("vendor workout provider must not be empty")
        params: dict[str, Any] | None = None
        if normalized_provider == "polar":
            params = {
                "samples": samples,
                "zones": zones,
                "route": route,
            }
        payload = await self._get(
            f"/api/v1/providers/{normalized_provider}/users/{user_id}/workouts/{workout_id}",
            params=params,
        )
        if normalized_provider == "suunto":
            return self._suunto_workout_detail(payload)
        if not isinstance(payload, Mapping):
            raise OWClientError(
                "open-wearables returned an invalid vendor workout detail response"
            )
        return dict(payload)

    # ------------------------------------------------------------------
    # Timeseries (routes/v1/timeseries.py — cursor pagination)
    # ------------------------------------------------------------------

    async def get_timeseries(
        self,
        user_id: str,
        start_time: str,
        end_time: str,
        types: list[str],
        *,
        resolution: Resolution = "raw",
        cursor: str | None = None,
        limit: int = TIMESERIES_MAX_LIMIT,
    ) -> dict[str, Any]:
        """GET /api/v1/users/{user_id}/timeseries — granular series samples."""
        params: dict[str, Any] = {
            "start_time": start_time,
            "end_time": end_time,
            "types": types,
            "resolution": resolution,
            "limit": limit,
        }
        if cursor is not None:
            params["cursor"] = cursor
        return await self._get(f"/api/v1/users/{user_id}/timeseries", params=params)

    async def collect_timeseries(
        self,
        user_id: str,
        start_time: str,
        end_time: str,
        types: list[str],
        *,
        resolution: Resolution = "raw",
        max_pages: int = TIMESERIES_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        """All timeseries samples in a window, following cursor pagination."""
        rows, _truncated = await self.collect_timeseries_tracked(
            user_id,
            start_time,
            end_time,
            types,
            resolution=resolution,
            max_pages=max_pages,
        )
        return rows

    async def collect_timeseries_tracked(
        self,
        user_id: str,
        start_time: str,
        end_time: str,
        types: list[str],
        *,
        resolution: Resolution = "raw",
        max_pages: int = TIMESERIES_MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Like :meth:`collect_timeseries`, plus a truncation flag.

        The second element is True when the page cap stopped the fetch with a
        live cursor remaining — consumers that aggregate over the whole window
        (e.g. the insight templates) must not present truncated data as the
        full picture.
        """
        return await self._collect_cursor(
            lambda cursor: self.get_timeseries(
                user_id,
                start_time,
                end_time,
                types,
                resolution=resolution,
                cursor=cursor,
            ),
            max_pages=max_pages,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _suunto_workout_detail(payload: Any) -> dict[str, Any]:
        """Validate and unwrap Suunto's single-workout response envelope."""
        if not isinstance(payload, Mapping):
            raise OWPayloadError(
                "open-wearables returned an invalid Suunto workout detail response"
            )
        error = payload.get("error")
        if error not in (None, "", False):
            raise OWPayloadError(
                "open-wearables returned a Suunto workout detail error"
            )
        detail = payload.get("payload")
        if not isinstance(detail, Mapping):
            raise OWPayloadError(
                "open-wearables returned an invalid Suunto workout detail payload"
            )
        return dict(detail)

    @staticmethod
    def _vendor_workout_rows(
        payload: dict[str, Any] | list[dict[str, Any]],
        *,
        provider: str,
    ) -> list[dict[str, Any]]:
        """Normalize known provider list envelopes without altering rows."""
        values: Any = payload
        if isinstance(payload, dict):
            if provider == "suunto":
                error = payload.get("error")
                if error not in (None, "", False):
                    raise OWPayloadError(
                        "open-wearables returned a Suunto workout error"
                    )
                values = payload.get("payload")
            else:
                for field in ("data", "records", "activities", "payload"):
                    candidate = payload.get(field)
                    if isinstance(candidate, list):
                        values = candidate
                        break
        if not isinstance(values, list) or any(
            not isinstance(row, Mapping) for row in values
        ):
            raise OWPayloadError(
                "open-wearables returned invalid vendor workout rows"
            )
        return [dict(row) for row in values]

    @staticmethod
    def _vendor_workout_continuation(
        payload: dict[str, Any] | list[dict[str, Any]],
    ) -> bool:
        """Detect continuation tokens the route cannot currently replay."""
        if not isinstance(payload, Mapping):
            return False
        for field in ("next_token", "nextToken"):
            if payload.get(field):
                return True
        pagination = payload.get("pagination")
        return isinstance(pagination, Mapping) and any(
            pagination.get(field)
            for field in ("next", "next_cursor", "next_token", "nextToken")
        )

    async def _collect_cursor(
        self, fetch_page, *, max_pages: int = MAX_PAGES
    ) -> tuple[list[dict[str, Any]], bool]:
        """Drain a cursor-paginated endpoint (``pagination.next_cursor``).

        Returns ``(rows, truncated)``; ``truncated`` is True when the page cap
        was reached while the backend still reported a next cursor.
        """
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            payload = await fetch_page(cursor)
            data = payload.get("data", [])
            rows.extend(data)
            cursor = payload.get("pagination", {}).get("next_cursor")
            if not cursor or not data:
                return rows, False
        if cursor:
            logger.warning(
                "cursor pagination stopped at max_pages=%d with more data available",
                max_pages,
            )
        return rows, cursor is not None


async def resolve_single_user_id(client: Any, settings: Settings) -> str:
    """The single open-wearables user this deployment reads (one policy).

    Shared by every consumer (MCP tools, trigger sweep, energy persist,
    insight recompute) so they can never disagree about the subject —
    open-wearables is multi-user and ``GET /users`` returns every user the
    key sees, newest first, so "take users[0]" silently reads someone else's
    data the moment a second account exists.

    Order: ``Settings.ow_user_id`` -> ``HEALTHMES_OW_USER_ID`` env var ->
    auto-discovery via ``GET /api/v1/users``, accepted only when the key sees
    exactly one user. Anything else raises :class:`LookupError` with the
    remedy in the message.

    ``client`` only needs a ``list_users(limit=...)`` method; sync fakes are
    supported alongside the real async :class:`OWClient`.
    """
    configured = getattr(settings, "ow_user_id", None)
    if configured:
        return str(configured)
    env_value = os.environ.get("HEALTHMES_OW_USER_ID")
    if env_value:
        return env_value
    payload = client.list_users(limit=2)
    if inspect.isawaitable(payload):
        payload = await payload
    if not isinstance(payload, Mapping):
        items = []
    else:
        items = payload.get("items") or payload.get("data") or []
    if (
        isinstance(items, list)
        and len(items) == 1
        and isinstance(items[0], Mapping)
        and items[0].get("id")
    ):
        return str(items[0]["id"])
    count = len(items) if isinstance(items, list) else 0
    raise LookupError(
        "Cannot determine the open-wearables user id: set HEALTHMES_OW_USER_ID "
        f"(API key currently sees {count} users; auto-discovery needs exactly one)."
    )
