"""Tests for the MCP server surface: tool inventory and the /mcp ASGI app."""

import httpx
from fastapi import FastAPI

from healthmes.mcp_server.server import build_mcp_http_app

TRANCHE_1_TOOLS = {
    "get_health_scores",
    "get_daily_readiness_context",
    "get_personal_baselines",
    "list_tasks",
    "upsert_task",
    "get_schedule",
    "propose_schedule_blocks",
    "log_food",
    "record_decision",
}
TRANCHE_2_TOOLS = {
    "get_cognitive_energy_forecast",
    "get_stress_timeline",
    "compare_impact",
}
TRANCHE_3_TOOLS = {
    "create_medical_record",
    "list_medical_records",
}
EXPECTED_TOOLS = TRANCHE_1_TOOLS | TRANCHE_2_TOOLS | TRANCHE_3_TOOLS

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class TestToolInventory:
    async def test_exactly_the_tranche_1_2_and_3_tools_are_registered(self, mcp_client):
        tools = await mcp_client.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS

    async def test_every_tool_has_a_decision_oriented_description(self, mcp_client):
        for tool in await mcp_client.list_tools():
            assert tool.description, f"{tool.name} is missing a description"
            assert len(tool.description) > 40, f"{tool.name} description too thin"


class TestHttpApp:
    def test_endpoint_route_is_exactly_slash_mcp(self):
        app = build_mcp_http_app()
        assert [route.path for route in app.routes] == ["/mcp"]
        assert callable(app.lifespan)  # composition root must run this

    async def test_streamable_http_initialize_over_asgi(self):
        """POST /mcp initialize handshakes once the lifespan is running."""
        app = build_mcp_http_app()
        async with app.lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://healthmes.test"
            ) as client:
                response = await client.post("/mcp", json=_INITIALIZE, headers=_MCP_HEADERS)
        assert response.status_code == 200
        assert response.headers.get("mcp-session-id")
        assert '"serverInfo"' in response.text
        assert '"healthmes"' in response.text

    async def test_documented_fastapi_mount_recipe(self):
        """The exact wiring recorded in needs.app_wiring keeps both surfaces."""
        mcp_app = build_mcp_http_app()
        app = FastAPI(lifespan=mcp_app.lifespan)

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        app.mount("", mcp_app)

        async with mcp_app.lifespan(mcp_app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://healthmes.test"
            ) as client:
                health_response = await client.get("/health")
                mcp_response = await client.post(
                    "/mcp", json=_INITIALIZE, headers=_MCP_HEADERS
                )
        assert health_response.status_code == 200  # FastAPI routes keep precedence
        assert mcp_response.status_code == 200
        assert mcp_response.headers.get("mcp-session-id")
