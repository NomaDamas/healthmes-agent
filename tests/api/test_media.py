"""Tests for the media capture endpoints (issue #10): upload → store → serve.

Covers the contract the companion apps build against: multipart upload with
uuid server-side naming, the size cap and content-type allowlist, traversal
resistance of the serving route, and the auth matrix (bearer for everything,
the derived viewer ``?token=`` for GET only — so decision/report pages can
embed media).
"""

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.app import create_app
from healthmes.config import Settings

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 32
MEDIA_PATH_RE = re.compile(r"^media/\d{4}/\d{2}/[0-9a-f]{32}\.[a-z0-9]+$")

TOKEN = "test-api-token-123"


def _upload(client, *, content=JPEG_BYTES, content_type="image/jpeg", filename="cat.jpg", **kw):
    return client.post("/v1/media", files={"file": (filename, content, content_type)}, **kw)


def _stored_files(settings) -> list:
    media_root = settings.data_dir / "media"
    if not media_root.exists():
        return []
    return [path for path in media_root.rglob("*") if path.is_file()]


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_upload_stores_file_and_returns_media_path(client, settings):
    response = _upload(client, filename="IMG_0001 evil/../name.jpg")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"media_path", "content_type", "bytes"}
    assert MEDIA_PATH_RE.fullmatch(body["media_path"]), body["media_path"]
    assert body["content_type"] == "image/jpeg"
    assert body["bytes"] == len(JPEG_BYTES)
    # The client filename is never trusted or reused (uuid naming).
    assert "IMG_0001" not in body["media_path"]
    stored = settings.data_dir / body["media_path"]
    assert stored.read_bytes() == JPEG_BYTES


def test_upload_then_fetch_round_trip(client):
    media_path = _upload(client).json()["media_path"]

    fetched = client.get(f"/v1/media/{media_path}")

    assert fetched.status_code == 200
    assert fetched.content == JPEG_BYTES
    assert fetched.headers["content-type"] == "image/jpeg"
    assert fetched.headers["cache-control"] == "private, max-age=86400, immutable"

    # The leading "media/" segment may be dropped (path relative to the root).
    bare = client.get(f"/v1/media/{media_path.removeprefix('media/')}")
    assert bare.status_code == 200
    assert bare.content == JPEG_BYTES


@pytest.mark.parametrize(
    ("declared", "canonical", "extension"),
    [
        ("image/jpg", "image/jpeg", ".jpg"),
        ("image/heic", "image/heic", ".heic"),
        ("image/webp", "image/webp", ".webp"),
        ("image/png", "image/png", ".png"),
        ("audio/x-m4a", "audio/mp4", ".m4a"),
        ("audio/m4a", "audio/mp4", ".m4a"),
        ("audio/mp3", "audio/mpeg", ".mp3"),
        ("audio/mpeg", "audio/mpeg", ".mp3"),
        ("audio/ogg; codecs=opus", "audio/ogg", ".ogg"),
        ("audio/wave", "audio/wav", ".wav"),
    ],
)
def test_upload_normalises_content_type_aliases(client, declared, canonical, extension):
    response = _upload(client, content=b"payload-bytes", content_type=declared, filename="x")

    assert response.status_code == 201
    body = response.json()
    assert body["content_type"] == canonical
    assert body["media_path"].endswith(extension)


@pytest.mark.parametrize(
    "declared",
    ["application/pdf", "text/plain", "application/octet-stream", "image/svg+xml", "video/mp4"],
)
def test_upload_rejects_disallowed_content_type(client, settings, declared):
    response = _upload(client, content_type=declared)

    assert response.status_code == 415
    error = response.json()["error"]
    assert error["code"] == "unsupported_media_type"
    assert "image/jpeg" in error["detail"]["allowed"]
    assert _stored_files(settings) == []


def test_upload_size_cap_rejects_and_removes_partial_file(settings):
    capped = settings.model_copy(update={"media_max_upload_bytes": 1000})
    with TestClient(create_app(capped)) as client:
        too_big = _upload(client, content=b"x" * 1001)
        assert too_big.status_code == 413
        error = too_big.json()["error"]
        assert error["code"] == "payload_too_large"
        assert error["detail"] == {"max_bytes": 1000}
        assert _stored_files(capped) == []

        # Exactly at the cap is accepted.
        at_cap = _upload(client, content=b"x" * 1000)
        assert at_cap.status_code == 201
        assert at_cap.json()["bytes"] == 1000


def _asgi_post_media(app, headers, body: bytes = b""):
    """POST /v1/media at the raw ASGI level, tracking body reads.

    TestClient cannot fabricate a Content-Length that disagrees with the
    body, and it hides whether the app pulled body bytes; calling the ASGI
    app directly proves the size gate acts BEFORE any ``receive()``.
    """
    import anyio

    messages: list[dict] = []
    receive_calls: list[str] = []

    async def receive():
        receive_calls.append("http.request")
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/media",
        "raw_path": b"/v1/media",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    anyio.run(app, scope, receive, send)

    status_code = next(m["status"] for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status_code, payload, receive_calls


def test_oversized_declared_content_length_is_rejected_before_any_body_read(settings):
    # The DoS the cap exists for (config.py: "keeping a LAN peer from filling
    # the disk"): multipart parsing spools file parts to the temp dir, so the
    # 413 must fire off the Content-Length header, before the body streams.
    import json

    capped = settings.model_copy(update={"media_max_upload_bytes": 1000})
    app = create_app(capped)

    status_code, payload, receive_calls = _asgi_post_media(
        app,
        headers=[
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=deadbeefcafe"),
            (b"content-length", str(5 * 1024 * 1024 * 1024).encode()),  # a "5 GB" body
        ],
    )

    assert status_code == 413
    assert json.loads(payload)["error"]["code"] == "payload_too_large"
    assert receive_calls == []  # not one body byte was pulled from the socket
    assert _stored_files(capped) == []


def test_upload_without_content_length_is_411(settings):
    # Chunked transfer has no up-front size to gate on; both companion apps
    # send fixed-length bodies, so requiring Content-Length costs nothing.
    import json

    app = create_app(settings)

    status_code, payload, receive_calls = _asgi_post_media(
        app,
        headers=[
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=deadbeefcafe"),
        ],
    )

    assert status_code == 411
    assert json.loads(payload)["error"]["code"] == "length_required"
    assert receive_calls == []


def test_multi_megabyte_body_is_refused_at_the_header_gate(settings):
    # Through the regular client: httpx declares the true Content-Length, so
    # a 5 MiB upload against a 1000-byte cap dies at the header check.
    capped = settings.model_copy(update={"media_max_upload_bytes": 1000})
    with TestClient(create_app(capped)) as client:
        response = _upload(client, content=b"x" * (5 * 1024 * 1024))

        assert response.status_code == 413
        assert response.json()["error"]["detail"] == {"max_bytes": 1000}
        assert _stored_files(capped) == []


def test_upload_without_file_field_is_422(client, settings):
    # The handler parses the form itself (no UploadFile route parameter — see
    # upload_media), so the missing-field error is the envelope, not
    # FastAPI's validation shape.
    no_file = client.post("/v1/media", data={"note": "no file here"})
    assert no_file.status_code == 422
    assert no_file.json()["error"]["code"] == "missing_file"

    wrong_field = client.post(
        "/v1/media", files={"attachment": ("x.jpg", JPEG_BYTES, "image/jpeg")}
    )
    assert wrong_field.status_code == 422
    assert wrong_field.json()["error"]["code"] == "missing_file"

    assert _stored_files(settings) == []


def test_upload_rejects_empty_file(client, settings):
    response = _upload(client, content=b"")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_file"
    assert _stored_files(settings) == []


def test_default_cap_is_15_mib():
    assert Settings.model_fields["media_max_upload_bytes"].default == 15 * 1024 * 1024


# ---------------------------------------------------------------------------
# Serving: unknown files, foreign extensions, traversal resistance
# ---------------------------------------------------------------------------


def test_fetch_unknown_file_is_404_envelope(client):
    response = client.get("/v1/media/2099/01/0123456789abcdef0123456789abcdef.jpg")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_fetch_unknown_extension_falls_back_to_octet_stream(client, settings):
    # Files written by other capture paths (Telegram skill) live in the same
    # tree and may carry extensions outside the upload allowlist.
    blob = settings.data_dir / "media" / "2026" / "07" / "note.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"opaque-bytes")

    response = client.get("/v1/media/2026/07/note.bin")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == b"opaque-bytes"


@pytest.mark.parametrize(
    "attack",
    [
        "%2e%2e/secret.txt",  # ../secret.txt (encoded so httpx cannot pre-normalise)
        "media/%2e%2e/secret.txt",  # media/../secret.txt
        "%2e%2e/%2e%2e/etc/passwd",
        "media/2026/%2e%2e/%2e%2e/secret.txt",
        "..%5csecret.txt",  # backslash variant
        "media%5c..%5csecret.txt",
        ".hidden",  # dotfiles are never served
        "media/.hidden",
        "media//secret.txt",  # empty segment
        "%00secret.txt",  # NUL byte
    ],
)
def test_fetch_rejects_path_tricks(client, settings, attack):
    # Canary files that a successful escape or dotfile read would expose.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "secret.txt").write_text("canary-outside-media")
    media_root = settings.data_dir / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / ".hidden").write_text("canary-dotfile")

    response = client.get(f"/v1/media/{attack}")

    assert response.status_code == 404, attack
    assert b"canary" not in response.content


def test_fetch_does_not_follow_symlinks_out_of_the_media_root(client, settings):
    (settings.data_dir / "media" / "2026" / "07").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "secret.txt").write_text("canary-symlink-target")
    link = settings.data_dir / "media" / "2026" / "07" / "innocent.jpg"
    link.symlink_to(settings.data_dir / "secret.txt")

    response = client.get("/v1/media/2026/07/innocent.jpg")

    assert response.status_code == 404
    assert b"canary" not in response.content


# ---------------------------------------------------------------------------
# Auth matrix (token configured): bearer everywhere, viewer token GET-only
# ---------------------------------------------------------------------------


@pytest.fixture
def secured_client(settings):
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    # The media routes never touch the database, so no schema setup is needed.
    with TestClient(create_app(secured)) as client:
        yield client


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_upload_requires_bearer_token(secured_client):
    anonymous = _upload(secured_client)
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthorized"

    # The derived viewer credential is read-only: it must never upload.
    viewer = _upload(secured_client, params={"token": viewer_token(TOKEN)})
    assert viewer.status_code == 401

    authorized = _upload(secured_client, headers=bearer())
    assert authorized.status_code == 201


def test_fetch_accepts_bearer_or_derived_viewer_token(secured_client):
    media_path = _upload(secured_client, headers=bearer()).json()["media_path"]
    url = f"/v1/media/{media_path}"

    assert secured_client.get(url).status_code == 401
    assert secured_client.get(url, params={"token": "wrong"}).status_code == 401
    assert secured_client.get(url, headers=bearer()).status_code == 200
    # Viewer pages embed media via <img>/<audio>, which cannot send headers.
    assert secured_client.get(url, params={"token": viewer_token(TOKEN)}).status_code == 200


def test_bare_media_collection_path_is_not_viewer_readable(secured_client):
    # Only "/v1/media/<something>" is on the viewer surface; the collection
    # URL itself stays bearer-only like the rest of /v1.
    response = secured_client.get("/v1/media", params={"token": viewer_token(TOKEN)})
    assert response.status_code in (401, 405)
