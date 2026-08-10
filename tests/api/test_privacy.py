from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.app import create_app


def test_privacy_notice_is_public_without_the_api_token(settings) -> None:
    secured = settings.model_copy(update={"api_token": SecretStr("private-api-token")})

    with TestClient(create_app(secured)) as client:
        response = client.get("/privacy")

    assert response.status_code == 200
    assert "WHOOP" in response.text
    assert "Calendar" in response.text
    assert "sleep, recovery, cycles, workouts, and profile" in response.text
    assert "body measurements" not in response.text
    assert "private-api-token" not in response.text
    assert viewer_token("private-api-token") not in response.text
