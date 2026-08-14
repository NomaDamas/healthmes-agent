"""REST facade for the unified HealthMes input control plane."""

from __future__ import annotations

from fastapi import APIRouter, Request

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


def _registry(request: Request) -> InputSourceRegistry:
    return InputSourceRegistry(settings=request.app.state.settings)


def _translate_error(exc: InputSourceRegistryError) -> APIError:
    status_code = 404 if exc.code == "input_source_not_found" else 422
    return APIError(status_code, exc.code, exc.message)


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
    session: SessionDep,
) -> InputSourceDescriptor:
    try:
        return _registry(request).get(session, source_id)
    except InputSourceRegistryError as exc:
        raise _translate_error(exc) from exc


@router.put("/{source_id}/settings", response_model=InputSourceDescriptor)
def put_input_settings(
    source_id: str,
    body: InputSettingsUpdate,
    request: Request,
    session: SessionDep,
) -> InputSourceDescriptor:
    try:
        return _registry(request).update(session, source_id, body)
    except InputSourceRegistryError as exc:
        raise _translate_error(exc) from exc
