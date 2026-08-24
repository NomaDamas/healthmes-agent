"""Setup readiness and one-time Apple pairing adapter."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.app import create_app
from healthmes.pairing import PairingGrantExpired, PairingGrantStore

TOKEN = "setup-test-api-token-32-characters"


def _secured_client(settings):
    secured = settings.model_copy(
        update={"api_token": SecretStr(TOKEN)}
    )
    return TestClient(
        create_app(secured),
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    )


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _code(pairing_url: str) -> str:
    return parse_qs(urlsplit(pairing_url).query)["code"][0]


def test_readiness_composes_existing_input_and_runtime_controls(client) -> None:
    response = client.get("/v1/setup/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"] == "action_required"
    assert {check["key"] for check in payload["checks"]} == {
        "instance",
        "public_pairing",
        "healthkit",
        "open_wearables",
        "calendar_google",
        "calendar_icloud",
        "decision_runtime",
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert "test-ow-api-key" not in response.text


def test_readiness_does_not_mark_configured_placeholders_ready(client) -> None:
    response = client.get("/v1/setup/readiness")
    checks = {check["key"]: check for check in response.json()["checks"]}

    assert checks["healthkit"]["state"] == "action_required"
    assert checks["open_wearables"]["state"] == "action_required"
    assert checks["decision_runtime"]["state"] == "action_required"
    assert checks["calendar_google"]["state"] == "action_required"
    assert checks["calendar_icloud"]["state"] == "action_required"


def test_readiness_optional_calendars_do_not_block_required_setup(settings) -> None:
    secured = settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "public_base_url": "https://healthmes.example",
            "ow_user_id": "0b6f3a52-8c1d-4e2a-9f10-2a5b7c9d1e3f",
        }
    )
    with TestClient(
        create_app(secured),
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        client.app.state.decision_engine = object()
        accepted = client.post(
            "/v1/ingest/healthkit",
            content=b"{}",
            headers={
                **_bearer(),
                "Content-Type": "application/json",
            },
        )
        assert accepted.status_code == 202
        response = client.get(
            "/v1/setup/readiness",
            headers=_bearer(),
        )

    checks = {check["key"]: check for check in response.json()["checks"]}
    assert checks["calendar_google"]["state"] == "action_required"
    assert checks["calendar_icloud"]["state"] == "action_required"
    assert response.json()["overall"] == "ready"


def test_pairing_grant_requires_server_token(client) -> None:
    response = client.post(
        "/v1/setup/pairing/grants",
        json={"audience": "mac"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "pairing_token_unconfigured"


def test_pairing_exchange_is_one_time_and_qr_never_contains_token(settings) -> None:
    with _secured_client(settings) as client:
        anonymous_issue = client.post(
            "/v1/setup/pairing/grants",
            json={"audience": "mac"},
        )
        assert anonymous_issue.status_code == 401

        issued = client.post(
            "/v1/setup/pairing/grants",
            json={"audience": "mac"},
            headers=_bearer(),
        )
        assert issued.status_code == 201
        grant = issued.json()
        assert grant["base_url"] == "http://127.0.0.1:8100"
        assert TOKEN not in issued.text
        assert "token=" not in grant["pairing_url"]
        assert issued.headers["Cache-Control"] == "no-store"

        code = _code(grant["pairing_url"])
        exchanged = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": code},
        )
        assert exchanged.status_code == 200
        assert exchanged.json() == {
            "base_url": "http://127.0.0.1:8100",
            "token": TOKEN,
        }
        assert exchanged.headers["Cache-Control"] == "no-store"
        assert exchanged.headers["Referrer-Policy"] == "no-referrer"

        reused = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": code},
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "pairing_code_consumed"


def test_phone_grant_is_pinned_to_configured_https_origin(settings) -> None:
    secured = settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "public_base_url": "https://healthmes.tailnet.example/base/",
        }
    )
    with TestClient(
        create_app(secured),
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        issued = client.post(
            "/v1/setup/pairing/grants",
            json={"audience": "phone"},
            headers=_bearer(),
        )
        assert issued.status_code == 201
        assert issued.json()["base_url"] == (
            "https://healthmes.tailnet.example/base"
        )

        exchanged = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": _code(issued.json()["pairing_url"])},
        )
        assert exchanged.json()["base_url"] == (
            "https://healthmes.tailnet.example/base"
        )


def test_phone_grant_rejects_insecure_or_credentialed_public_url(settings) -> None:
    for public_base_url in (
        "http://192.0.2.10:8100",
        "https://user:password@healthmes.example",
    ):
        secured = settings.model_copy(
            update={
                "api_token": SecretStr(TOKEN),
                "public_base_url": public_base_url,
            }
        )
        with TestClient(
            create_app(secured),
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43123),
        ) as client:
            response = client.post(
                "/v1/setup/pairing/grants",
                json={"audience": "phone"},
                headers=_bearer(),
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == (
            "secure_pairing_origin_required"
        )


def test_expired_grant_returns_gone_and_files_are_owner_only(tmp_path) -> None:
    store = PairingGrantStore(tmp_path)
    issued_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    code, _ = store.issue(
        "https://healthmes.example",
        now=issued_at,
        ttl=timedelta(seconds=1),
    )

    active = list((tmp_path / "pairing-grants" / "active").iterdir())
    assert len(active) == 1
    if os.name != "nt":
        assert active[0].stat().st_mode & 0o077 == 0

    try:
        store.consume(code, now=issued_at + timedelta(seconds=2))
    except PairingGrantExpired:
        pass
    else:
        raise AssertionError("expired grant was accepted")


def test_exchange_keeps_grant_active_when_token_disappears(settings) -> None:
    secured = settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "public_base_url": "https://healthmes.example",
        }
    )
    with TestClient(
        create_app(secured),
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        issued = client.post(
            "/v1/setup/pairing/grants",
            json={"audience": "phone"},
            headers=_bearer(),
        )
        assert issued.status_code == 201
        client.app.state.settings = secured.model_copy(
            update={"api_token": SecretStr("")}
        )
        failed = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": _code(issued.json()["pairing_url"])},
        )
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "pairing_token_unconfigured"

        client.app.state.settings = secured
        retried = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": _code(issued.json()["pairing_url"])},
        )

    assert retried.status_code == 200
    assert retried.json()["token"] == TOKEN


def test_unknown_exchange_does_not_require_bearer_but_fails_closed(settings) -> None:
    with _secured_client(settings) as client:
        response = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": "a" * 43},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "pairing_code_not_found"
