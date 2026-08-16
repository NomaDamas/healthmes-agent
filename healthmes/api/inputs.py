"""REST facade for the unified HealthMes input control plane."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request, Response, status

from healthmes.api.errors import APIError
from healthmes.inputs import (
    InputSettingsUpdate,
    InputSourceDescriptor,
    InputSourceRegistry,
    InputSourceRegistryError,
    InputSourcesOut,
)
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/inputs", tags=["inputs"])

_REVISION_VALUE = r"sha256:[0-9a-f]{64}"
_IF_MATCH_PATTERN = re.compile(
    rf'^(?:"(?P<quoted>{_REVISION_VALUE})"|(?P<bare>{_REVISION_VALUE}))$'
)
_IF_MATCH_OPENAPI_PARAMETER = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": (
        "Current input descriptor revision from GET /v1/inputs/{source_id}. "
        "Accepts an exact sha256 tag in quoted or unquoted form."
    ),
    "schema": {
        "type": "string",
        "pattern": rf'^(?:"{_REVISION_VALUE}"|{_REVISION_VALUE})$',
        "examples": [
            "sha256:" + ("0" * 64),
            '"sha256:' + ("0" * 64) + '"',
        ],
    },
}


def _registry(request: Request) -> InputSourceRegistry:
    return InputSourceRegistry(settings=request.app.state.settings)


def _translate_error(exc: InputSourceRegistryError) -> APIError:
    status_code = {
        "input_source_not_found": status.HTTP_404_NOT_FOUND,
        "input_settings_revision_conflict": status.HTTP_409_CONFLICT,
    }.get(exc.code, status.HTTP_422_UNPROCESSABLE_CONTENT)
    return APIError(status_code, exc.code, exc.message, detail=exc.detail)


def _parse_if_match(raw_values: list[str]) -> str:
    if not raw_values:
        raise APIError(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "input_settings_revision_required",
            "If-Match is required for input settings updates.",
        )
    if len(raw_values) != 1:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "input_settings_revision_invalid",
            "If-Match must contain exactly one input descriptor revision.",
        )
    raw_value = raw_values[0]
    match = _IF_MATCH_PATTERN.fullmatch(raw_value.strip())
    if match is None:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "input_settings_revision_invalid",
            (
                "If-Match must be one exact quoted or unquoted "
                "sha256:<64 lowercase hex characters> revision."
            ),
        )
    return match.group("quoted") or match.group("bare")


def _set_revision_etag(response: Response, revision: str) -> None:
    response.headers["ETag"] = f'"{revision}"'


@router.get("", response_model=InputSourcesOut)
def list_inputs(
    request: Request,
    session: SessionDep,
) -> InputSourcesOut:
    return InputSourcesOut(
        sources=list(_registry(request).list(session)),
    )


@router.get("/{source_id}", response_model=InputSourceDescriptor)
def get_input(
    source_id: str,
    request: Request,
    response: Response,
    session: SessionDep,
) -> InputSourceDescriptor:
    try:
        descriptor = _registry(request).get(session, source_id)
    except InputSourceRegistryError as exc:
        raise _translate_error(exc) from exc
    _set_revision_etag(response, descriptor.revision)
    return descriptor


@router.put(
    "/{source_id}/settings",
    response_model=InputSourceDescriptor,
    description=(
        "Atomically update input settings when If-Match equals the current "
        "descriptor revision. A semantically identical update returns 200 "
        "and preserves the revision, so that revision remains reusable."
    ),
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Malformed If-Match revision.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "The input descriptor revision is stale.",
        },
        status.HTTP_428_PRECONDITION_REQUIRED: {
            "description": "The required If-Match revision is missing.",
        },
    },
)
def put_input_settings(
    source_id: str,
    body: InputSettingsUpdate,
    request: Request,
    response: Response,
    session: SessionDep,
) -> InputSourceDescriptor:
    expected_revision = _parse_if_match(
        request.headers.getlist("if-match")
    )
    try:
        descriptor = _registry(request).update(
            session,
            source_id,
            body,
            expected_revision=expected_revision,
        )
    except InputSourceRegistryError as exc:
        raise _translate_error(exc) from exc
    _set_revision_etag(response, descriptor.revision)
    return descriptor
