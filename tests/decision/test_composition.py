from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision import composition


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
    assert captured["fingerprint_key"] == (
        b"test-decision-correlation-secret-32-bytes"
    )
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
        match=(
            "decision_hermes_profile_path, "
            "decision_hermes_runtime_manifest_path, "
            "decision_hermes_attestation_key_path are required"
        ),
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
            "decision_hermes_runtime_manifest_path": (
                tmp_path / "runtime-manifest.json"
            ),
            "decision_hermes_attestation_key_path": (
                tmp_path / "runtime-attestation.key"
            ),
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


def test_production_composition_rejects_short_correlation_key(
    settings,
    tmp_path,
) -> None:
    configured = settings.model_copy(
        update={
            "decision_hermes_base_url": "http://127.0.0.1:8644",
            "decision_hermes_model": "decision-model",
            "decision_hermes_provider": "decision-provider",
            "decision_hermes_profile_path": tmp_path / "config.yaml",
            "decision_hermes_runtime_manifest_path": (
                tmp_path / "runtime-manifest.json"
            ),
            "decision_hermes_attestation_key_path": (
                tmp_path / "runtime-attestation.key"
            ),
            "decision_correlation_secret": SecretStr("too-short"),
        }
    )

    with pytest.raises(
        ValueError,
        match="decision_correlation_secret.*at least 32 bytes",
    ):
        composition.build_configured_decision_engine(
            settings=configured,
            session_factory=sessionmaker[Session](),
        )


def test_production_composition_uses_attested_responses_transport(
    settings,
    monkeypatch,
    tmp_path,
) -> None:
    captured_transport: dict[str, object] = {}
    captured_engine: dict[str, object] = {}
    expected_engine = object()
    search_service = object()

    class StubTransport:
        def __init__(self, **kwargs) -> None:
            captured_transport.update(kwargs)

    def build_responses(**kwargs):
        captured_engine.update(kwargs)
        return expected_engine

    monkeypatch.setattr(
        composition,
        "HermesHttpResponsesTransport",
        StubTransport,
    )
    monkeypatch.setattr(
        composition,
        "build_healthmes_responses_decision_engine",
        build_responses,
    )
    configured = settings.model_copy(
        update={
            "decision_hermes_base_url": "http://127.0.0.1:8645",
            "decision_hermes_model": "decision-model",
            "decision_hermes_provider": "decision-provider",
            "decision_hermes_profile_path": tmp_path / "config.yaml",
            "decision_hermes_runtime_manifest_path": (
                tmp_path / "runtime-manifest.json"
            ),
            "decision_hermes_attestation_key_path": (
                tmp_path / "runtime-attestation.key"
            ),
            "decision_hermes_api_key": SecretStr("k" * 64),
            "decision_correlation_secret": SecretStr("c" * 64),
        }
    )

    engine = composition.build_configured_decision_engine(
        settings=configured,
        session_factory=sessionmaker[Session](),
        search_service=search_service,  # type: ignore[arg-type]
    )

    assert engine is expected_engine
    assert captured_transport["base_url"] == "http://127.0.0.1:8645"
    attestation = captured_transport["runtime_attestation"]
    assert attestation.manifest_path == tmp_path / "runtime-manifest.json"
    assert attestation.attestation_key_path == (
        tmp_path / "runtime-attestation.key"
    )
    assert captured_engine["transport"].__class__ is StubTransport
    assert captured_engine["search_service"] is search_service
    assert captured_engine["fingerprint_key"] == b"c" * 64
