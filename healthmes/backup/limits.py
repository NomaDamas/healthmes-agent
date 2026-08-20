"""Bounded resource policy for encrypted snapshot creation and restore."""

from __future__ import annotations

import math
from dataclasses import dataclass

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotResourceLimits:
    """Hard limits that keep malformed snapshots from exhausting the node."""

    max_encrypted_bytes: int = 512 * _MIB
    max_decrypted_bytes: int = 768 * _MIB
    max_members: int = 100_000
    max_member_bytes: int = 1024 * _MIB
    max_expanded_bytes: int = 4 * 1024 * _MIB
    max_identity_depth: int = 128
    identity_traversal_timeout_seconds: float = 300.0
    max_compression_ratio: float = 100.0
    min_free_bytes: int = 256 * _MIB

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_encrypted_bytes,
            self.max_decrypted_bytes,
            self.max_members,
            self.max_member_bytes,
            self.max_expanded_bytes,
            self.max_identity_depth,
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in integer_limits
        ):
            raise ValueError("snapshot resource limits must be positive")
        if (
            not isinstance(self.min_free_bytes, int)
            or isinstance(self.min_free_bytes, bool)
            or self.min_free_bytes < 0
        ):
            raise ValueError("snapshot free-space reserve must be non-negative")
        if (
            not isinstance(
                self.identity_traversal_timeout_seconds,
                (int, float),
            )
            or isinstance(
                self.identity_traversal_timeout_seconds,
                bool,
            )
            or not math.isfinite(
                self.identity_traversal_timeout_seconds
            )
            or self.identity_traversal_timeout_seconds <= 0
        ):
            raise ValueError(
                "snapshot identity traversal timeout must be a positive "
                "finite number"
            )
        if (
            not isinstance(self.max_compression_ratio, (int, float))
            or isinstance(self.max_compression_ratio, bool)
            or not math.isfinite(self.max_compression_ratio)
            or self.max_compression_ratio <= 1
        ):
            raise ValueError("snapshot compression ratio limit must be greater than one")
        if self.max_member_bytes > self.max_expanded_bytes:
            raise ValueError("snapshot member limit cannot exceed expanded archive limit")


def limits_from_settings(settings) -> SnapshotResourceLimits:
    """Build limits from typed Settings while supporting slim test doubles."""

    defaults = SnapshotResourceLimits()
    return SnapshotResourceLimits(
        max_encrypted_bytes=getattr(
            settings,
            "backup_max_encrypted_bytes",
            defaults.max_encrypted_bytes,
        ),
        max_decrypted_bytes=getattr(
            settings,
            "backup_max_decrypted_bytes",
            defaults.max_decrypted_bytes,
        ),
        max_members=getattr(
            settings,
            "backup_max_archive_members",
            defaults.max_members,
        ),
        max_member_bytes=getattr(
            settings,
            "backup_max_member_bytes",
            defaults.max_member_bytes,
        ),
        max_expanded_bytes=getattr(
            settings,
            "backup_max_expanded_bytes",
            defaults.max_expanded_bytes,
        ),
        max_identity_depth=getattr(
            settings,
            "backup_max_identity_depth",
            defaults.max_identity_depth,
        ),
        identity_traversal_timeout_seconds=getattr(
            settings,
            "backup_identity_traversal_timeout_seconds",
            defaults.identity_traversal_timeout_seconds,
        ),
        max_compression_ratio=getattr(
            settings,
            "backup_max_compression_ratio",
            defaults.max_compression_ratio,
        ),
        min_free_bytes=getattr(
            settings,
            "backup_min_free_bytes",
            defaults.min_free_bytes,
        ),
    )
