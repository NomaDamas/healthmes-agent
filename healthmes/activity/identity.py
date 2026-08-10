"""Stable, privacy-minimized source identities for device adapters."""

from __future__ import annotations

import hashlib


def device_namespace(device_id: str) -> str:
    """Return a stable opaque namespace without copying a device ID into keys."""
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]


def scoped_source_record_id(
    *,
    prefix: str,
    device_id: str,
    source_record_id: str,
) -> str:
    """Namespace a source-local record ID so multiple devices cannot collide."""
    source_digest = hashlib.sha256(source_record_id.encode("utf-8")).hexdigest()
    return f"{prefix}:{device_namespace(device_id)}:{source_digest}"
