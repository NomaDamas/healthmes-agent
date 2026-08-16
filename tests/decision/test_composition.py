from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr
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


def test_configured_composition_selects_responses_runtime(
    settings,
    monkeypatch,
) -> None:
    transport = object()
    search_service = object()
    expected_engine = object()
    captured: dict[str, object] = {}

    def build_responses(**kwargs):
        captured.update(kwargs)
        return expected_engine

    monkeypatch.setattr(
        composition,
        "build_healthmes_responses_decision_engine",
        build_responses,
    )
    configured = settings.model_copy(
        update={
            "decision_hermes_base_url": "http://127.0.0.1:8644",
            "decision_hermes_model": "decision-model",
            "decision_hermes_provider": "decision-provider",
        }
    )

    engine = composition.build_configured_decision_engine(
        settings=configured,
        session_factory=sessionmaker[Session](),
        transport=transport,  # type: ignore[arg-type]
        search_service=search_service,  # type: ignore[arg-type]
    )

    assert engine is expected_engine
    assert captured["transport"] is transport
    assert captured["search_service"] is search_service
    assert captured["model"] == "decision-model"
    assert captured["provider"] == "decision-provider"
    assert captured["owns_search_service"] is False


def test_production_composition_requires_rendered_decision_profile(
    settings,
) -> None:
    configured = settings.model_copy(
        update={
            "decision_hermes_base_url": "http://127.0.0.1:8644",
            "decision_hermes_model": "decision-model",
            "decision_hermes_provider": "decision-provider",
            "decision_hermes_profile_path": None,
        }
    )

    with pytest.raises(
        ValueError,
        match="decision_hermes_profile_path is required",
    ):
        composition.build_configured_decision_engine(
            settings=configured,
            session_factory=sessionmaker[Session](),
        )


def test_production_composition_requires_dedicated_api_key(
    settings,
    tmp_path,
) -> None:
    configured = settings.model_copy(
        update={
            "decision_hermes_base_url": "http://127.0.0.1:8644",
            "decision_hermes_model": "decision-model",
            "decision_hermes_provider": "decision-provider",
            "decision_hermes_profile_path": tmp_path / "config.yaml",
            "decision_hermes_api_key": SecretStr(""),
        }
    )

    with pytest.raises(
        ValueError,
        match="decision_hermes_api_key is required",
    ):
        composition.build_configured_decision_engine(
            settings=configured,
            session_factory=sessionmaker[Session](),
        )
