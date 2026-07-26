"""Pairing payload + QR rendering (healthmes/pairing.py)."""

from pydantic import SecretStr

from healthmes.pairing import build_pairing_url, render_terminal_qr


def test_pairing_url_encodes_base_and_token(settings):
    s2 = settings.model_copy(
        update={
            "public_base_url": "https://healthmes.example.com/",
            "api_token": SecretStr("tok/with+special=chars"),
        }
    )
    url = build_pairing_url(s2)
    assert url.startswith("healthmes://pair?url=https%3A%2F%2Fhealthmes.example.com")
    assert "token=tok%2Fwith%2Bspecial%3Dchars" in url
    assert "/" not in url.split("url=")[1].split("&")[0]


def test_pairing_url_omits_empty_token(settings):
    url = build_pairing_url(settings)  # conftest: api_token=""
    assert "token=" not in url
    assert url == "healthmes://pair?url=http%3A%2F%2Fhealthmes.test%3A8100"


def test_terminal_qr_renders(settings):
    block = render_terminal_qr(build_pairing_url(settings))
    assert len(block.splitlines()) > 10  # a real QR block, not an empty string
