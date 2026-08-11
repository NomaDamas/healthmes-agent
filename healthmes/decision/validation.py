"""Strict validation helpers for data crossing decision trust boundaries."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel


def _plain_value(value: Any) -> Any:
    """Remove trusted Pydantic instances before validating their contents."""

    if isinstance(value, BaseModel):
        return _plain_value(
            value.model_dump(mode="python", round_trip=True)
        )
    if isinstance(value, dict):
        return {
            _plain_value(key): _plain_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain_value(item) for item in value)
    if isinstance(value, set):
        return {_plain_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_plain_value(item) for item in value)
    return deepcopy(value)


def strict_model_validate[T: BaseModel](
    model: type[T],
    value: Any,
) -> T:
    """Re-run every nested validator and detach caller-owned mutable state."""

    return model.model_validate(_plain_value(value)).model_copy(deep=True)
