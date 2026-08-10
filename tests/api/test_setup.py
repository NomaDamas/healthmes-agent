"""One-click setup exchange and readiness API."""

from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.app import create_app
from healthmes.pairing import issue_pairing_grant
from healthmes.store import Base, RawIngestEvent
from healthmes.store.session import get_engine

TOKEN = "setup-test-api-token"


@contextmanager
def setup_client(settings, tmp_path):
    configured = settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "data_dir": tmp_path,
            "public_base_url": "https://healthmes.example.com",
        }
    )
    with TestClient(create_app(configured)) as client:
        Base.metadata.create_all(get_engine())
        yield client, configured


def bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_pairing_exchange_is_open_but_code_is_one_time(settings, tmp_path):
    with setup_client(settings, tmp_path) as (client, configured):
        grant = issue_pairing_grant(configured)
        code = parse_qs(urlsplit(grant.deep_link).query)["code"][0]

        exchanged = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": code},
        )
        assert exchanged.status_code == 200
        assert exchanged.json() == {
            "base_url": "https://healthmes.example.com",
            "token": TOKEN,
            "expires_in": 0,
        }

        replay = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": code},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "pairing_consumed"


def test_pairing_exchange_rejects_tampering(settings, tmp_path):
    with setup_client(settings, tmp_path) as (client, configured):
        grant = issue_pairing_grant(configured)
        code = parse_qs(urlsplit(grant.deep_link).query)["code"][0]

        response = client.post(
            "/v1/setup/pairing/exchange",
            json={"code": f"{code[:-1]}x"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_pairing_code"


def test_readiness_reports_real_healthkit_scheduler_and_https_state(
    client, session, settings
):
    response = client.get("/v1/setup/readiness")
    assert response.status_code == 200
    checks = {row["key"]: row for row in response.json()["checks"]}
    assert checks["healthkit_ingest"]["state"] == "action_required"
    assert checks["scheduler"]["state"] == "action_required"
    assert checks["public_https"]["state"] == "action_required"

    session.add(
        RawIngestEvent(
            received_at=datetime.now(UTC),
            source="healthkit-bridge",
            content_type="application/json",
            path="raw_ingest/test.json",
            size_bytes=2,
            sha256="a" * 64,
            parse_status="parsed",
            forward_status="queued",
        )
    )
    session.commit()
    client.app.state.settings = settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "scheduler_enabled": True,
            "native_alert_delivery": True,
            "public_base_url": "https://healthmes.example",
        }
    )

    ready = client.get("/v1/setup/readiness").json()
    checks = {row["key"]: row for row in ready["checks"]}
    assert checks["health"]["state"] == "ready"
    assert checks["healthkit_ingest"]["state"] == "ready"
    assert checks["scheduler"]["state"] == "ready"
    assert checks["notifications"]["state"] == "ready"
    assert checks["public_https"]["state"] == "ready"


def test_readiness_rejects_https_loopback_for_phone_and_watch(
    client,
    settings,
):
    for public_base_url in (
        "https://localhost:8100",
        "https://127.0.0.1:8100",
        "https://127.1:8100",
        "https://127.0.1:8100",
        "https://2130706433:8100",
    ):
        client.app.state.settings = settings.model_copy(
            update={
                "api_token": SecretStr(TOKEN),
                "public_base_url": public_base_url,
            }
        )

        checks = {
            row["key"]: row
            for row in client.get(
                "/v1/setup/readiness",
                headers=bearer(),
            ).json()["checks"]
        }

        assert checks["public_https"]["state"] == "action_required"
        assert "non-loopback HTTPS URL" in checks["public_https"]["detail"]


def test_readiness_requires_bearer_and_reports_components(settings, tmp_path):
    with setup_client(settings, tmp_path) as (client, _configured):
        assert client.get("/v1/setup/readiness").status_code == 401

        response = client.get("/v1/setup/readiness", headers=bearer())
        assert response.status_code == 200
        body = response.json()
        assert body["overall"] == "action_required"
        assert {check["key"] for check in body["checks"]} == {
            "instance",
            "health",
            "healthkit_ingest",
            "calendar_google",
            "calendar_icloud",
            "notifications",
            "scheduler",
            "public_https",
            "storage",
        }
