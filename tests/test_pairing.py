"""One-time pairing grants and QR rendering."""

import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from healthmes import pairing as pairing_mod
from healthmes.pairing import (
    PairingGrantCorrupt,
    PairingGrantStore,
    build_pairing_url,
    render_terminal_qr,
)


def test_pairing_url_encodes_base_and_one_time_code(settings):
    s2 = settings.model_copy(
        update={
            "public_base_url": "https://healthmes.example.com/",
            "api_token": SecretStr("tok/with+special=chars"),
        }
    )
    url = build_pairing_url(
        s2,
        now=datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    assert url.startswith("healthmes://pair?url=https%3A%2F%2Fhealthmes.example.com")
    assert "token=" not in url
    assert "tok%2Fwith%2Bspecial%3Dchars" not in url
    code = parse_qs(urlsplit(url).query)["code"][0]
    assert PairingGrantStore(s2.data_dir).consume(
        code,
        now=datetime(2026, 8, 24, 12, 1, tzinfo=UTC),
    ) == (
        "https://healthmes.example.com"
    )
    assert "/" not in url.split("url=")[1].split("&")[0]


def test_pairing_url_rejects_insecure_non_loopback_origin(settings):
    try:
        build_pairing_url(settings)
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("insecure non-loopback pairing was accepted")


def test_terminal_qr_renders(settings):
    loopback = settings.model_copy(
        update={"public_base_url": "http://127.0.0.1:8100"}
    )
    block = render_terminal_qr(build_pairing_url(loopback))
    assert len(block.splitlines()) > 10  # a real QR block, not an empty string


@pytest.mark.parametrize(
    ("base_url", "ttl"),
    (
        ("http://192.0.2.10:8100", timedelta(minutes=1)),
        ("https://user:password@healthmes.example", timedelta(minutes=1)),
        ("https://healthmes.example", timedelta(0)),
        ("https://healthmes.example", timedelta(minutes=6)),
    ),
)
def test_pairing_store_rejects_invalid_origin_or_ttl(
    tmp_path,
    base_url,
    ttl,
):
    with pytest.raises(ValueError):
        PairingGrantStore(tmp_path).issue(base_url, ttl=ttl)


def test_pairing_store_rejects_tampered_record_semantics(tmp_path):
    store = PairingGrantStore(tmp_path)
    issued_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    code, _ = store.issue(
        "https://healthmes.example",
        now=issued_at,
    )
    active = next((tmp_path / "pairing-grants" / "active").iterdir())
    payload = json.loads(active.read_text(encoding="utf-8"))
    payload["base_url"] = "http://192.0.2.10:8100"
    active.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    if os.name != "nt":
        active.chmod(0o600)

    with pytest.raises(PairingGrantCorrupt):
        store.consume(code, now=issued_at + timedelta(seconds=1))

    assert active.exists()


def test_pairing_issue_and_consume_fsync_mutated_directories(
    tmp_path,
    monkeypatch,
):
    synced: list = []
    monkeypatch.setattr(
        pairing_mod,
        "_fsync_directory",
        lambda path: synced.append(path),
    )
    store = PairingGrantStore(tmp_path)
    code, _ = store.issue("https://healthmes.example")
    active = tmp_path / "pairing-grants" / "active"
    consumed = tmp_path / "pairing-grants" / "consumed"

    assert active in synced
    synced.clear()

    assert store.consume(code) == "https://healthmes.example"
    assert synced == [active, consumed]
