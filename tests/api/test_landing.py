"""Landing shell (GET /) carries no credentials.

The static landing must never put a token on its links. The viewer token is an
HMAC of the API token that a browser client cannot derive, so propagating a
``?token=`` from the visitor's own URL onto same-origin links (what the old
inline script did) could only leak a pasted *full* bearer token onto the /v1
links. These tests pin that the served markup holds no token-propagation hook
and that no credential — raw API token or derived viewer token — ever appears
in a link. The client/settings fixtures come from tests/api/conftest.py.
"""

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.app import create_app

_HREF = re.compile(r'href="([^"]*)"')


def _hrefs(html: str) -> list[str]:
    return _HREF.findall(html)


@pytest.fixture
def client(app):
    """Landing served by the shared api-test app (tokenless settings)."""
    with TestClient(app) as test_client:
        yield test_client


def test_landing_returns_200_with_surface_links(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    hrefs = _hrefs(response.text)
    # Real-page sanity: the surface cards are present to assert against.
    assert "/decisions" in hrefs
    assert "/v1/briefing/glance" in hrefs


def test_landing_links_carry_no_token_and_no_propagation_hook(client) -> None:
    html = client.get("/").text
    # No link carries a token, and the token-propagation hook/script is gone.
    for href in _hrefs(html):
        assert "token=" not in href
    assert "data-token-link" not in html
    assert "token=" not in html  # nothing anywhere hands a link a token
    # The /v1 link — the leak vector — carries no query credential at all.
    v1_hrefs = [href for href in _hrefs(html) if href.startswith("/v1/")]
    assert v1_hrefs  # it is present...
    for href in v1_hrefs:
        assert "?" not in href and "token" not in href


def test_landing_does_not_rewrite_links_from_visitor_token(client) -> None:
    """A ?token= in the visitor's own URL must NOT be copied onto the links
    (the inline rewrite script is deleted) nor echoed anywhere in the page."""
    leaked = "leaked-full-bearer-token-value"
    response = client.get(f"/?token={leaked}")
    assert response.status_code == 200
    for href in _hrefs(response.text):
        assert "token=" not in href
    assert leaked not in response.text


def test_landing_never_echoes_the_configured_api_token(settings) -> None:
    """Even with a real API token configured, GET / (an OPEN_PATH) renders
    neither the raw API token nor its derived viewer token."""
    secret = "super-secret-raw-api-token-value"
    tokened = settings.model_copy(update={"api_token": SecretStr(secret)})
    with TestClient(create_app(tokened)) as test_client:
        response = test_client.get("/")
    assert response.status_code == 200
    assert secret not in response.text
    assert viewer_token(secret) not in response.text
    for href in _hrefs(response.text):
        assert "token=" not in href
