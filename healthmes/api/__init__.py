"""REST surface of the HealthMes service (docs/PLAN.md Phase 1 + §2).

Route module conventions follow
``vendor/open-wearables/backend/app/api/routes/v1/``. Domain routes live under
``/v1/...``; human-facing paths additionally live outside the prefix:
the decision pages at ``/decisions`` (linked from alerts, docs/PLAN.md §8.5),
the energy forecast at ``/cognitive-energy/forecast`` (docs/PLAN.md §3 — each
also has a ``/v1`` twin), and the weekly report at ``/reports/weekly``
(docs/PLAN.md §8.5). The companion-app glance briefing is at
``/v1/briefing/glance`` (issue #7); the native capture upload/serve pair is
``/v1/media`` and the alert history ``/v1/alerts`` (issue #10).

Wiring: the app factory calls :func:`include_all`, which installs the shared
error-envelope handlers and every router below.
"""

from fastapi import APIRouter, FastAPI

from healthmes.api import (
    alerts,
    app_usage,
    briefing,
    decisions,
    energy,
    food,
    goals,
    insights,
    media,
    medical,
    reports,
    schedule,
    tasks,
)
from healthmes.api.errors import install_error_handlers

__all__ = ["routers", "include_all"]

# Order is cosmetic only (OpenAPI docs grouping).
routers: list[APIRouter] = [
    goals.router,
    tasks.router,
    schedule.router,
    food.router,
    medical.router,
    media.router,
    insights.router,
    energy.router,
    decisions.router,
    app_usage.router,
    briefing.router,
    alerts.router,
    reports.router,
]


def include_all(app: FastAPI) -> None:
    """Install the error-envelope handlers and mount every API router.

    Idempotent: a second call on the same app is a no-op, so the app factory
    and test fixtures can both call it safely.
    """
    if getattr(app.state, "healthmes_api_included", False):
        return
    app.state.healthmes_api_included = True
    install_error_handlers(app)
    for router in routers:
        app.include_router(router)
