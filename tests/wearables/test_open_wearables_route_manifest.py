from __future__ import annotations

import ast
from pathlib import Path

from healthmes.wearables.open_wearables_routes import (
    OPEN_WEARABLES_V1_CLASSIFICATION_COUNTS,
    OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES,
    OPEN_WEARABLES_V1_ROUTE_BY_IDENTITY,
    OPEN_WEARABLES_V1_ROUTE_COUNT,
    OPEN_WEARABLES_V1_ROUTE_COVERAGE,
    OpenWearablesRouteClassification,
    OpenWearablesRouteIdentity,
)
from healthmes.wearables.search import WEARABLE_DETAIL_CAPABILITIES

EXPECTED_HEALTH_ROUTE_CAPABILITIES = {
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.events",
        method="GET",
        path="/users/{user_id}/events/workouts",
    ): ("wearable.workouts",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.events",
        method="GET",
        path="/users/{user_id}/events/sleep",
    ): ("wearable.sleep-sessions",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.events",
        method="GET",
        path="/users/{user_id}/events/menstrual-cycles",
    ): ("wearable.menstrual-cycles",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.health_scores",
        method="GET",
        path="/users/{user_id}/health-scores",
    ): (
        "wearable.health-scores",
        "wearable.whoop-recovery-package",
    ),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.summaries",
        method="GET",
        path="/users/{user_id}/summaries/activity",
    ): ("wearable.summaries",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.summaries",
        method="GET",
        path="/users/{user_id}/summaries/sleep",
    ): ("wearable.summaries",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.summaries",
        method="GET",
        path="/users/{user_id}/summaries/recovery",
    ): ("wearable.summaries",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.summaries",
        method="GET",
        path="/users/{user_id}/summaries/body",
    ): ("wearable.body-summary",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.timeseries",
        method="GET",
        path="/users/{user_id}/timeseries",
    ): ("wearable.timeseries",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.vendor_workouts",
        method="GET",
        path="/{provider}/users/{user_id}/workouts",
    ): ("wearable.provider-workouts",),
    OpenWearablesRouteIdentity(
        module="app.api.routes.v1.vendor_workouts",
        method="GET",
        path="/{provider}/users/{user_id}/workouts/{workout_id}",
    ): ("wearable.provider-workout-detail",),
}

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
V1_ROUTE_ROOT = (
    REPOSITORY_ROOT
    / "vendor"
    / "open-wearables"
    / "backend"
    / "app"
    / "api"
    / "routes"
    / "v1"
)
V1_MODULE_PREFIX = "app.api.routes.v1"
FASTAPI_ROUTE_METHODS = frozenset(
    {
        "DELETE",
        "GET",
        "HEAD",
        "OPTIONS",
        "PATCH",
        "POST",
        "PUT",
        "TRACE",
    }
)


def _literal_route_path(
    decorator: ast.Call,
    *,
    source_path: Path,
    line_number: int,
) -> str:
    assert decorator.args, (
        f"{source_path}:{line_number}: route decorator has no path argument"
    )
    try:
        route_path = ast.literal_eval(decorator.args[0])
    except (ValueError, TypeError) as exc:
        raise AssertionError(
            f"{source_path}:{line_number}: route path must be a string literal"
        ) from exc
    assert isinstance(route_path, str), (
        f"{source_path}:{line_number}: route path must be a string literal"
    )
    return route_path


def _discover_v1_route_decorators() -> set[OpenWearablesRouteIdentity]:
    discovered: set[OpenWearablesRouteIdentity] = set()

    for source_path in sorted(V1_ROUTE_ROOT.rglob("*.py")):
        module_suffix = (
            source_path.relative_to(V1_ROUTE_ROOT)
            .with_suffix("")
            .as_posix()
            .replace("/", ".")
        )
        module = f"{V1_MODULE_PREFIX}.{module_suffix}"
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        )

        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.AsyncFunctionDef, ast.FunctionDef),
            ):
                continue

            for decorator in node.decorator_list:
                if (
                    not isinstance(decorator, ast.Call)
                    or not isinstance(decorator.func, ast.Attribute)
                ):
                    continue
                method = decorator.func.attr.upper()
                if method not in FASTAPI_ROUTE_METHODS:
                    continue
                assert (
                    isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "router"
                ), (
                    f"{source_path}:{decorator.lineno}: FastAPI route "
                    "decorator must use the module's `router`"
                )

                identity = OpenWearablesRouteIdentity(
                    module=module,
                    method=method,
                    path=_literal_route_path(
                        decorator,
                        source_path=source_path,
                        line_number=decorator.lineno,
                    ),
                )
                assert identity not in discovered, (
                    f"duplicate FastAPI route identity: {identity}"
                )
                discovered.add(identity)

    return discovered


def test_manifest_classifies_every_v1_fastapi_route_decorator() -> None:
    discovered = _discover_v1_route_decorators()
    classified = set(OPEN_WEARABLES_V1_ROUTE_BY_IDENTITY)

    assert classified == discovered, (
        "Open Wearables v1 route manifest drifted.\n"
        f"Unclassified upstream routes: {sorted(discovered - classified)!r}\n"
        f"Stale manifest routes: {sorted(classified - discovered)!r}"
    )


def test_manifest_has_unique_routes_reasons_and_expected_counts() -> None:
    assert len(OPEN_WEARABLES_V1_ROUTE_COVERAGE) == len(
        OPEN_WEARABLES_V1_ROUTE_BY_IDENTITY
    )
    assert OPEN_WEARABLES_V1_ROUTE_COUNT == 115
    assert all(
        entry.reason.strip() for entry in OPEN_WEARABLES_V1_ROUTE_COVERAGE
    )
    assert OPEN_WEARABLES_V1_CLASSIFICATION_COUNTS == {
        OpenWearablesRouteClassification.EXPOSED_USER_HEALTH_READ: 11,
        OpenWearablesRouteClassification.INTERNAL_AVAILABILITY_IDENTITY_METADATA: 7,
        OpenWearablesRouteClassification.INTENTIONALLY_EXCLUDED: 97,
    }


def test_every_health_read_is_connected_to_a_runtime_capability() -> None:
    mapped = {
        entry.identity: entry.capabilities
        for entry in OPEN_WEARABLES_V1_ROUTE_COVERAGE
        if entry.classification
        is OpenWearablesRouteClassification.EXPOSED_USER_HEALTH_READ
    }

    assert mapped == EXPECTED_HEALTH_ROUTE_CAPABILITIES
    assert all(
        not entry.capabilities
        for entry in OPEN_WEARABLES_V1_ROUTE_COVERAGE
        if entry.classification
        is not OpenWearablesRouteClassification.EXPOSED_USER_HEALTH_READ
    )
    assert (
        OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES
        == WEARABLE_DETAIL_CAPABILITIES
    )
