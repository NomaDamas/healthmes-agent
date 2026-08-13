from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision import RuntimeMetadata, composition


class _Runtime:
    metadata = RuntimeMetadata(
        runtime="scripted",
        model="composition-test-v1",
    )


@pytest.mark.parametrize(
    "failure_stage",
    ("finalizer", "engine"),
)
def test_composition_failure_closes_created_agent(
    monkeypatch,
    failure_stage,
) -> None:
    created: list[SimpleNamespace] = []

    class StubAgent:
        def __init__(self, **_kwargs) -> None:
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    class StubFinalizer:
        def __init__(self, **_kwargs) -> None:
            if failure_stage == "finalizer":
                raise RuntimeError("finalizer construction failed")

    class StubEngine:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("engine construction failed")

    monkeypatch.setattr(
        composition,
        "HealthMesDecisionAgent",
        StubAgent,
    )
    monkeypatch.setattr(
        composition,
        "DecisionFinalizer",
        StubFinalizer,
    )
    monkeypatch.setattr(
        composition,
        "HealthMesDecisionEngine",
        StubEngine,
    )

    with pytest.raises(RuntimeError, match="construction failed"):
        composition.build_healthmes_decision_engine(
            runtime=_Runtime(),  # type: ignore[arg-type]
            session_factory=sessionmaker[Session](),
            policy_resolver=lambda _request: None,  # type: ignore[return-value]
        )

    assert len(created) == 1
    assert created[0].closed is True
