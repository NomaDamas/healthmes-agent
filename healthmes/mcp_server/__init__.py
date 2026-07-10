"""HealthMes fastmcp server (Layer B "interpreted context" tools).

Mounted on the FastAPI app at /mcp (Streamable HTTP) and registered in the
Hermes ``mcp_servers`` config via url. See docs/PLAN.md section 1.5.

Modules:
- ``ow_client`` — read-only httpx client for the open-wearables REST v1 API
- ``interpret`` — deterministic baselines / z-scores / sleep debt / stress
- ``timeline`` — pure stress-interval building + calendar/app-usage context
  joins for ``get_stress_timeline`` (all in the user's local timezone)
- ``impact`` — pure before/after delta aggregation for ``compare_impact``
- ``server`` — the FastMCP instance, tool definitions, and the ASGI app
  factory ``build_mcp_http_app`` (import from there:
  ``from healthmes.mcp_server.server import mcp, build_mcp_http_app``).
"""
