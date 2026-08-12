"""Strict validation helpers for data crossing decision trust boundaries."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Any

from pydantic import BaseModel

_DEFAULT_MAX_JSON_BYTES = 2_000_000
_DEFAULT_MAX_JSON_DEPTH = 64
_DEFAULT_MAX_JSON_NODES = 20_000
_DEFAULT_MAX_JSON_SCALAR_BYTES = 256_000
_MAX_JSON_INTEGER_BITS = 4_096
_LOG10_2_UPPER_NUMERATOR = 30_103
_LOG10_2_UPPER_DENOMINATOR = 100_000


@dataclass(frozen=True, slots=True)
class NormalizedJson:
    """Detached exact-JSON tree and its already bounded encoding."""

    value: Any
    encoded: bytes


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


def normalize_untrusted_json(
    value: Any,
    *,
    max_bytes: int = _DEFAULT_MAX_JSON_BYTES,
    max_depth: int = _DEFAULT_MAX_JSON_DEPTH,
    max_nodes: int = _DEFAULT_MAX_JSON_NODES,
    max_scalar_bytes: int = _DEFAULT_MAX_JSON_SCALAR_BYTES,
    deadline: float | None = None,
) -> NormalizedJson:
    """Copy and encode bounded JSON without invoking caller-defined methods."""

    if (
        max_bytes < 1
        or max_depth < 0
        or max_nodes < 1
        or max_scalar_bytes < 1
    ):
        raise ValueError("JSON normalization limits must be positive")

    nodes_seen = 0
    encoded = bytearray()
    active_containers: set[int] = set()

    def ensure_before_deadline() -> None:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError

    def append(chunk: bytes) -> None:
        if len(encoded) + len(chunk) > max_bytes:
            raise ValueError("JSON value exceeds encoded size limit")
        encoded.extend(chunk)

    def encode_string(item: str) -> bytes:
        if len(item) > max_scalar_bytes:
            raise ValueError("JSON string exceeds scalar size limit")
        ensure_before_deadline()
        raw = str.encode(item, "utf-8")
        if len(raw) > max_scalar_bytes:
            raise ValueError("JSON string exceeds scalar size limit")
        del raw
        rendered = json.dumps(
            item,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        ensure_before_deadline()
        return rendered

    def integer_encoded_size_upper(item: int) -> int:
        if item == 0:
            digits = 1
        else:
            bits = int.bit_length(item)
            digits = (
                bits * _LOG10_2_UPPER_NUMERATOR
                + _LOG10_2_UPPER_DENOMINATOR
                - 1
            ) // _LOG10_2_UPPER_DENOMINATOR
        return digits + int(item < 0)

    def normalize(item: Any, *, depth: int) -> Any:
        nonlocal nodes_seen

        ensure_before_deadline()
        nodes_seen += 1
        if nodes_seen > max_nodes or depth > max_depth:
            raise ValueError("JSON value exceeds normalization limits")

        item_type = type(item)
        if item is None:
            append(b"null")
            return item
        if item_type is bool:
            append(b"true" if item else b"false")
            return item
        if item_type is int:
            if int.bit_length(item) > _MAX_JSON_INTEGER_BITS:
                raise ValueError("JSON integer exceeds normalization limits")
            if (
                len(encoded) + integer_encoded_size_upper(item)
                > max_bytes
            ):
                raise ValueError("JSON value exceeds encoded size limit")
            ensure_before_deadline()
            rendered = str(item).encode("ascii")
            ensure_before_deadline()
            append(rendered)
            return item
        if item_type is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            ensure_before_deadline()
            rendered = json.dumps(
                item,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
            ensure_before_deadline()
            append(rendered)
            return item
        if item_type is str:
            append(encode_string(item))
            return item
        if item_type is list:
            container_id = id(item)
            if container_id in active_containers:
                raise ValueError("JSON containers must not be cyclic")
            active_containers.add(container_id)
            normalized_list: list[Any] = []
            append(b"[")
            try:
                for index, child in enumerate(list.__iter__(item)):
                    if index:
                        append(b",")
                    normalized_list.append(
                        normalize(child, depth=depth + 1)
                    )
                append(b"]")
                return normalized_list
            finally:
                active_containers.discard(container_id)
        if item_type is dict:
            container_id = id(item)
            if container_id in active_containers:
                raise ValueError("JSON containers must not be cyclic")
            active_containers.add(container_id)
            normalized: dict[str, Any] = {}
            append(b"{")
            try:
                for index, (key, child) in enumerate(dict.items(item)):
                    ensure_before_deadline()
                    if type(key) is not str:
                        raise TypeError(
                            "JSON object keys must be strings"
                        )
                    nodes_seen += 1
                    if nodes_seen > max_nodes:
                        raise ValueError(
                            "JSON value exceeds normalization limits"
                        )
                    if index:
                        append(b",")
                    append(encode_string(key))
                    append(b":")
                    normalized[key] = normalize(
                        child,
                        depth=depth + 1,
                    )
                append(b"}")
                return normalized
            finally:
                active_containers.discard(container_id)
        raise TypeError("JSON value must use exact built-in types")

    normalized = normalize(value, depth=0)
    ensure_before_deadline()
    return NormalizedJson(
        value=normalized,
        encoded=bytes(encoded),
    )


def strict_json_model_validate[T: BaseModel](
    model: type[T],
    value: Any,
) -> T:
    """Validate an untrusted JSON value without Pydantic scalar coercion."""

    normalized = (
        value
        if type(value) is NormalizedJson
        else normalize_untrusted_json(value)
    )
    return model.model_validate_json(
        normalized.encoded,
        strict=True,
    ).model_copy(deep=True)
