"""One-click setup exchange and readiness API."""

from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.app import create_app
from healthmes.pairing import issue_pairing_grant
from healthmes.store import Base
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
            "calendar_google",
            "calendar_icloud",
            "notifications",
            "storage",
        }
