"""Signed, expiring, one-time pairing grants."""

from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from healthmes.pairing import (
    PairingGrantConsumed,
    PairingGrantError,
    PairingGrantExpired,
    exchange_pairing_grant,
    issue_pairing_grant,
    render_terminal_qr,
)

TOKEN = "pairing-test-api-token"


def secured_settings(settings, tmp_path):
    return settings.model_copy(
        update={
            "public_base_url": "https://healthmes.example.com/",
            "api_token": SecretStr(TOKEN),
            "data_dir": tmp_path,
        }
    )


def code_from(deep_link: str) -> str:
    return parse_qs(urlsplit(deep_link).query)["code"][0]


def test_pairing_url_contains_one_time_code_not_bearer(settings, tmp_path):
    configured = secured_settings(settings, tmp_path)
    grant = issue_pairing_grant(configured, now=1_000)

    assert grant.deep_link.startswith(
        "healthmes://pair?url=https%3A%2F%2Fhealthmes.example.com"
    )
    assert "&code=" in grant.deep_link
    assert "token=" not in grant.deep_link
    assert TOKEN not in grant.deep_link
    assert grant.expires_at == 1_300


def test_pairing_grant_exchanges_exactly_once(settings, tmp_path):
    configured = secured_settings(settings, tmp_path)
    code = code_from(issue_pairing_grant(configured, now=1_000).deep_link)

    assert exchange_pairing_grant(configured, code, now=1_001) == TOKEN
    with pytest.raises(PairingGrantConsumed):
        exchange_pairing_grant(configured, code, now=1_002)


def test_pairing_grant_rejects_expired_code(settings, tmp_path):
    configured = secured_settings(settings, tmp_path)
    code = code_from(
        issue_pairing_grant(configured, now=1_000, ttl_seconds=10).deep_link
    )

    with pytest.raises(PairingGrantExpired):
        exchange_pairing_grant(configured, code, now=1_011)


def test_pairing_grant_rejects_tampering(settings, tmp_path):
    configured = secured_settings(settings, tmp_path)
    code = code_from(issue_pairing_grant(configured, now=1_000).deep_link)

    with pytest.raises(PairingGrantError):
        exchange_pairing_grant(configured, f"{code[:-1]}x", now=1_001)


def test_pairing_requires_authenticated_instance(settings, tmp_path):
    configured = settings.model_copy(
        update={
            "data_dir": tmp_path,
            "public_base_url": "https://healthmes.example.com",
        }
    )
    with pytest.raises(PairingGrantError):
        issue_pairing_grant(configured)


@pytest.mark.parametrize(
    "public_base_url",
    [
        "https://localhost:8100",
        "https://127.0.0.1:8100",
        "https://127.1:8100",
        "https://127.0.1:8100",
        "https://2130706433:8100",
    ],
)
def test_remote_pairing_rejects_loopback_public_url(
    settings,
    tmp_path,
    public_base_url,
):
    configured = secured_settings(settings, tmp_path).model_copy(
        update={"public_base_url": public_base_url}
    )

    with pytest.raises(PairingGrantError, match="non-loopback HTTPS"):
        issue_pairing_grant(configured, require_remote=True)


def test_local_mac_pairing_allows_authenticated_loopback(settings, tmp_path):
    configured = settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "data_dir": tmp_path,
            "public_base_url": "http://127.0.0.1:8100",
        }
    )

    grant = issue_pairing_grant(configured)

    assert grant.deep_link.startswith(
        "healthmes://pair?url=http%3A%2F%2F127.0.0.1%3A8100"
    )


def test_terminal_qr_renders(settings, tmp_path):
    configured = secured_settings(settings, tmp_path)
    grant = issue_pairing_grant(configured)
    block = render_terminal_qr(grant.deep_link)
    assert len(block.splitlines()) > 10
