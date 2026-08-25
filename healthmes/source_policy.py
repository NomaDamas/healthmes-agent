"""Stable bindings for owner-scoped input source switches."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.store import InputSourcePolicy

OPEN_WEARABLES_INPUT_SOURCE_ID = "wearable.open-wearables"


@dataclass(frozen=True, slots=True)
class InputSourcePolicyBinding:
    """One exact source-switch state captured before external work."""

    owner_principal_id: str
    source_id: str
    enabled: bool
    revision: int


class StaleInputSourcePolicyError(RuntimeError):
    """Raised when an external result no longer matches its source switch."""


def input_source_policy_binding(
    session: Session,
    *,
    owner_principal_id: str,
    source_id: str,
) -> InputSourcePolicyBinding:
    """Read one owner-scoped source switch without creating a default row."""

    owner = owner_principal_id.strip()
    source = source_id.strip().casefold()
    if not owner or len(owner) > 255:
        raise ValueError(
            "owner_principal_id must contain 1 to 255 characters"
        )
    if not source or len(source) > 255:
        raise ValueError("source_id must contain 1 to 255 characters")
    state = session.execute(
        select(
            InputSourcePolicy.enabled,
            InputSourcePolicy.revision,
        ).where(
            InputSourcePolicy.owner_principal_id == owner,
            InputSourcePolicy.source_id == source,
        )
    ).one_or_none()
    if state is None:
        return InputSourcePolicyBinding(
            owner_principal_id=owner,
            source_id=source,
            enabled=True,
            revision=0,
        )
    enabled, revision = state
    return InputSourcePolicyBinding(
        owner_principal_id=owner,
        source_id=source,
        enabled=bool(enabled),
        revision=int(revision),
    )


def assert_input_source_policy_binding(
    session: Session,
    expected: InputSourcePolicyBinding,
) -> None:
    """Reject a result if its source was disabled or changed after capture."""

    if not isinstance(expected, InputSourcePolicyBinding):
        raise TypeError(
            "expected source policy must be an InputSourcePolicyBinding"
        )
    current = input_source_policy_binding(
        session,
        owner_principal_id=expected.owner_principal_id,
        source_id=expected.source_id,
    )
    if not expected.enabled or not current.enabled or current != expected:
        raise StaleInputSourcePolicyError(
            "input source policy changed before result persistence"
        )


__all__ = [
    "InputSourcePolicyBinding",
    "OPEN_WEARABLES_INPUT_SOURCE_ID",
    "StaleInputSourcePolicyError",
    "assert_input_source_policy_binding",
    "input_source_policy_binding",
]
