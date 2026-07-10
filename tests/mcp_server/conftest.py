"""Fixtures for the MCP-server test suite.

No network, Docker, or credentials: open-wearables is faked with an
``httpx.MockTransport`` backend that mimics the vendor REST v1 envelopes
(``PaginatedResponse``/``OldPaginatedResponse``), and the healthmes store runs
on in-memory sqlite. Tools are exercised through the real fastmcp in-memory
client so input/output schemas are validated too.
"""

import datetime as dt
import json
import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastmcp import Client
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings
from healthmes.mcp_server import server as server_module
from healthmes.mcp_server.ow_client import OWClient
from healthmes.store import Base, create_db_engine

USER_ID = "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
API_KEY = "test-ow-api-key"
OW_BASE_URL = "http://open-wearables.test"

# Every mcp_env test runs pinned to a fixed non-UTC timezone (KST, UTC+9) so
# the local-tz joins of the tranche-2 tools are deterministic on any machine
# and accidental UTC/date coupling fails loudly. A fixed offset (not ZoneInfo)
# keeps the tests independent of the host tz database.
PINNED_TZ = dt.timezone(dt.timedelta(hours=9))

_USER_PATH = re.compile(rf"^/api/v1/users/{USER_ID}/(?P<suffix>.+)$")


def _parse_window_param(value: str | None) -> dt.datetime | None:
    """Parse start_date/end_date the way the vendor does (date -> midnight UTC)."""
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _paginated(data: list[dict], total: int | None = None, next_cursor: str | None = None) -> dict:
    """Vendor ``PaginatedResponse`` envelope (schemas/utils/pagination.py)."""
    return {
        "data": data,
        "pagination": {
            "next_cursor": next_cursor,
            "previous_cursor": None,
            "has_more": next_cursor is not None,
            "total_count": total if total is not None else len(data),
        },
        "metadata": {"sample_count": len(data)},
    }


class FakeOW:
    """Programmable fake of the open-wearables REST v1 API."""

    def __init__(self) -> None:
        self.users: list[dict] = [{"id": USER_ID, "email": "user@example.com"}]
        self.health_scores: list[dict] = []
        self.sleep_summaries: list[dict] = []
        self.recovery_summaries: list[dict] = []
        self.workouts: list[dict] = []
        self.timeseries: list[dict] = []
        self.requests: list[httpx.Request] = []
        # When set, health-scores pages are capped to this size so tests can
        # exercise the client's offset-pagination loop.
        self.max_page_size: int | None = None
        # When set, timeseries pages are capped to this size so tests can
        # exercise the cursor-pagination loop of collect_timeseries.
        self.timeseries_page_size: int | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # -- fixture builders --------------------------------------------------

    def add_score(
        self,
        category: str,
        provider: str,
        recorded_at: str,
        value: float | None,
        *,
        qualifier: str | None = None,
        components: dict | None = None,
    ) -> None:
        self.health_scores.append(
            {
                "id": str(uuid.uuid4()),
                "category": category,
                "provider": provider,
                "value": value,
                "qualifier": qualifier,
                "recorded_at": recorded_at,
                "zone_offset": None,
                "components": components,
                "data_source_id": None,
            }
        )

    def add_sleep_summary(self, date: str, **fields: object) -> None:
        self.sleep_summaries.append({"date": date, "source": {"provider": "oura"}, **fields})

    def add_recovery_summary(self, date: str, **fields: object) -> None:
        self.recovery_summaries.append({"date": date, "source": {"provider": "whoop"}, **fields})

    def add_workout(self, start_time: str, **fields: object) -> None:
        self.workouts.append(
            {
                "id": str(uuid.uuid4()),
                "type": fields.pop("type", "running"),
                "start_time": start_time,
                "end_time": fields.pop("end_time", start_time),
                "source": {"provider": "garmin"},
                **fields,
            }
        )

    def add_stress_sample(self, timestamp: str, value: float) -> None:
        """One garmin_stress_level timeseries sample (TimeSeriesSample shape)."""
        self.add_timeseries_sample(timestamp, "garmin_stress_level", value, unit="score")

    def add_timeseries_sample(
        self, timestamp: str, series_type: str, value: float, *, unit: str = "score"
    ) -> None:
        self.timeseries.append(
            {
                "timestamp": timestamp,
                "zone_offset": None,
                "type": series_type,
                "value": value,
                "unit": unit,
                "source": {"provider": "garmin"},
                "is_daily_total": False,
            }
        )

    # -- request handling ---------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-Open-Wearables-API-Key") != API_KEY:
            return httpx.Response(401, json={"detail": "Invalid API key"})

        path = request.url.path
        params = request.url.params
        if path == "/api/v1/users":
            return httpx.Response(
                200,
                json={
                    "items": self.users,
                    "total": len(self.users),
                    "page": 1,
                    "limit": int(params.get("limit", "20")),
                },
            )

        match = _USER_PATH.match(path)
        if not match:
            return httpx.Response(404, json={"detail": f"Not found: {path}"})
        suffix = match.group("suffix")

        if suffix == "health-scores":
            return self._health_scores_response(params)
        if suffix == "summaries/sleep":
            return httpx.Response(
                200, json=_paginated(self._filter_by_date(self.sleep_summaries, params))
            )
        if suffix == "summaries/recovery":
            return httpx.Response(
                200, json=_paginated(self._filter_by_date(self.recovery_summaries, params))
            )
        if suffix == "events/workouts":
            rows = self._filter_by_datetime(self.workouts, "start_time", params)
            return httpx.Response(200, json=_paginated(rows))
        if suffix == "timeseries":
            return self._timeseries_response(params)
        return httpx.Response(404, json={"detail": f"Not found: {path}"})

    def _timeseries_response(self, params: httpx.QueryParams) -> httpx.Response:
        """Vendor /timeseries route: types filter + cursor (offset-encoded) pages."""
        rows = self._filter_by_datetime(
            self.timeseries,
            "timestamp",
            params,
            start_param="start_time",
            end_param="end_time",
        )
        types = params.get_list("types")
        if types:
            rows = [row for row in rows if row["type"] in types]
        rows = sorted(rows, key=lambda row: str(row["timestamp"]))
        offset = int(params.get("cursor") or "0")
        limit = int(params.get("limit", "50"))
        if self.timeseries_page_size is not None:
            limit = min(limit, self.timeseries_page_size)
        page = rows[offset : offset + limit]
        has_more = offset + len(page) < len(rows)
        payload = _paginated(
            page,
            total=len(rows),
            next_cursor=str(offset + len(page)) if has_more else None,
        )
        return httpx.Response(200, json=payload)

    def _health_scores_response(self, params: httpx.QueryParams) -> httpx.Response:
        rows = self._filter_by_datetime(self.health_scores, "recorded_at", params)
        if params.get("category"):
            rows = [row for row in rows if row["category"] == params["category"]]
        if params.get("provider"):
            rows = [row for row in rows if row["provider"] == params["provider"]]
        offset = int(params.get("offset", "0"))
        limit = int(params.get("limit", "50"))
        if self.max_page_size is not None:
            limit = min(limit, self.max_page_size)
        page = rows[offset : offset + limit]
        payload = _paginated(page, total=len(rows))
        payload["pagination"]["has_more"] = offset + len(page) < len(rows)
        payload["pagination"]["next_cursor"] = None
        return httpx.Response(200, json=payload)

    @staticmethod
    def _filter_by_datetime(
        rows: list[dict],
        field: str,
        params: httpx.QueryParams,
        *,
        start_param: str = "start_date",
        end_param: str = "end_date",
    ) -> list[dict]:
        start = _parse_window_param(params.get(start_param))
        end = _parse_window_param(params.get(end_param))
        out = []
        for row in rows:
            at = dt.datetime.fromisoformat(str(row[field]).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=dt.UTC)
            if start is not None and at < start:
                continue
            if end is not None and at >= end:
                continue
            out.append(row)
        return out

    @staticmethod
    def _filter_by_date(rows: list[dict], params: httpx.QueryParams) -> list[dict]:
        start = _parse_window_param(params.get("start_date"))
        end = _parse_window_param(params.get("end_date"))
        out = []
        for row in rows:
            day = dt.date.fromisoformat(str(row["date"]))
            if start is not None and day < start.date():
                continue
            if end is not None and day >= (end.date() + dt.timedelta(days=1)):
                continue
            out.append(row)
        return out


@pytest.fixture
def ow_user_id() -> str:
    """The fixed open-wearables user id served by the fake backend."""
    return USER_ID


@pytest.fixture
def ow_api_key() -> str:
    """The API key the fake backend accepts."""
    return API_KEY


@pytest.fixture
def fake_ow() -> FakeOW:
    return FakeOW()


@pytest.fixture
def ow_client(fake_ow: FakeOW) -> OWClient:
    """A real OWClient wired to the fake backend via MockTransport."""
    return OWClient(base_url=OW_BASE_URL, api_key=API_KEY, transport=fake_ow.transport())


@pytest.fixture
def store_factory() -> Iterator[sessionmaker[Session]]:
    """Session factory over an in-memory sqlite store with the full schema."""
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autocommit=False, autoflush=False)
    engine.dispose()


@pytest.fixture
def mcp_env(
    fake_ow: FakeOW,
    ow_client: OWClient,
    store_factory: sessionmaker[Session],
    tmp_path,
) -> Iterator[FakeOW]:
    """Wire the MCP server's runtime state to the fakes for one test."""
    server_module.set_settings(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            ow_base_url=OW_BASE_URL,
            ow_api_key=API_KEY,
            public_base_url="http://healthmes.test:8100",
            data_dir=tmp_path / "data",
            scheduler_enabled=False,
            _env_file=None,
        )
    )
    server_module.set_ow_client(ow_client)
    server_module.set_session_factory(store_factory)
    server_module.set_ow_user_id(USER_ID)
    server_module.set_timezone(PINNED_TZ)
    yield fake_ow
    server_module.reset_runtime_state()


@pytest.fixture
def pinned_tz() -> dt.timezone:
    """The fixed local timezone the MCP env is pinned to (KST, UTC+9)."""
    return PINNED_TZ


@pytest.fixture
async def mcp_client(mcp_env: FakeOW) -> Client:
    """In-memory MCP client connected to the healthmes FastMCP server."""
    async with Client(server_module.mcp) as client:
        yield client


@pytest.fixture
def call_tool():
    """Async helper: call a tool and return its structured dict result.

    (A fixture rather than an importable helper because ``--import-mode=
    importlib`` makes conftest modules non-importable by design.)
    """

    async def call(client: Client, name: str, arguments: dict | None = None) -> dict:
        result = await client.call_tool(name, arguments or {})
        data = result.data if isinstance(result.data, dict) else result.structured_content
        assert isinstance(data, dict), f"tool {name} returned non-dict: {result!r}"
        # Belt and braces: results must be JSON-serializable (MCP wire format).
        json.dumps(data)
        return data

    return call
