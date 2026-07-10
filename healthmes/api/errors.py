"""Consistent error envelope for every REST error response.

All errors — domain errors (:class:`APIError`), FastAPI request-validation
failures, and plain ``HTTPException`` / router 404s — are serialised as::

    {"error": {"code": "<machine_code>", "message": "<human message>", "detail": ...}}

``install_error_handlers`` registers the handlers on the app; it is called by
:func:`healthmes.api.include_all` so the envelope stays consistent no matter
which router raised.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "APIError",
    "install_error_handlers",
    "error_body",
    "not_found",
    "invalid_transition",
]

# Machine codes for envelope consumers (Telegram skill scripts, web viewer).
_STATUS_CODE_NAMES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_error",
    status.HTTP_502_BAD_GATEWAY: "upstream_error",
}


class APIError(Exception):
    """A domain error carrying the envelope fields.

    Raise from any handler; the installed exception handler turns it into the
    standard JSON envelope with ``status_code``.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


def error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    """Build the envelope dict (single source of truth for its shape)."""
    return {"error": {"code": code, "message": message, "detail": detail}}


def not_found(resource: str, resource_id: Any) -> APIError:
    """404 envelope for a missing path resource."""
    return APIError(
        status.HTTP_404_NOT_FOUND,
        "not_found",
        f"{resource} {resource_id} not found",
    )


def invalid_transition(resource: str, current: str, requested: str) -> APIError:
    """409 envelope for a disallowed status transition."""
    return APIError(
        status.HTTP_409_CONFLICT,
        "invalid_transition",
        f"{resource} cannot transition from '{current}' to '{requested}'",
        detail={"current": current, "requested": requested},
    )


async def _api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(error_body(exc.code, exc.message, exc.detail)),
    )


async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=jsonable_encoder(
            error_body("validation_error", "Request validation failed", exc.errors())
        ),
    )


async def _http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_CODE_NAMES.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(error_body(code, str(exc.detail))),
        headers=getattr(exc, "headers", None),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the envelope handlers on ``app`` (idempotent)."""
    app.add_exception_handler(APIError, _api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
