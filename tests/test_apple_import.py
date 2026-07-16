"""Apple Health export upload (healthmes/apple_import.py).

Covers the file-shape contract (raw XML vs Health-app ZIP, localized inner
names, CDA companion exclusion), streaming/cap behavior, and the upload
request itself via a mock transport — no network, per tests/conftest.py
policy.
"""

import zipfile
from pathlib import Path

import httpx
import pytest

import healthmes.apple_import as apple_import
from healthmes.apple_import import (
    AppleImportError,
    import_apple_export,
    open_export_xml,
)

XML_BODY = b'<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="ko_KR"></HealthData>\n'


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _read_and_close(stream) -> bytes:
    try:
        return stream.read()
    finally:
        stream.close()


# --- open_export_xml ---------------------------------------------------------


def test_open_raw_xml(tmp_path):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)
    filename, stream, size = open_export_xml(xml)
    assert filename == "export.xml"
    assert size == len(XML_BODY)
    assert _read_and_close(stream) == XML_BODY


def test_open_zip_picks_main_export_over_cda(tmp_path):
    zip_path = tmp_path / "export.zip"
    _write_zip(
        zip_path,
        {
            "apple_health_export/export.xml": XML_BODY,
            "apple_health_export/export_cda.xml": b"<ClinicalDocument/>" * 10,
        },
    )
    filename, stream, size = open_export_xml(zip_path)
    assert filename == "export.xml"
    assert size == len(XML_BODY)
    assert _read_and_close(stream) == XML_BODY


def test_open_zip_localized_korean_export(tmp_path):
    zip_path = tmp_path / "내보내기.zip"
    _write_zip(zip_path, {"apple_health_export/내보내기.xml": XML_BODY})
    filename, stream, _size = open_export_xml(zip_path)
    assert filename == "내보내기.xml"
    assert _read_and_close(stream) == XML_BODY


def test_open_zip_without_xml_fails(tmp_path):
    zip_path = tmp_path / "notes.zip"
    _write_zip(zip_path, {"readme.txt": b"hi"})
    with pytest.raises(AppleImportError, match="no export XML"):
        open_export_xml(zip_path)


def test_open_corrupt_zip_fails_cleanly(tmp_path):
    # Intact end-of-central-directory (so is_zipfile says yes) but a smashed
    # local file header: reading the member raises BadZipFile, which must
    # surface as AppleImportError, not a raw traceback.
    good = tmp_path / "good.zip"
    _write_zip(good, {"apple_health_export/export.xml": XML_BODY})
    bad = tmp_path / "broken.zip"
    data = good.read_bytes()
    bad.write_bytes(b"XXXX" + data[4:])
    with pytest.raises(AppleImportError, match="cannot extract"):
        open_export_xml(bad)


def test_open_zip_cap_enforced_during_copy(tmp_path, monkeypatch):
    # Cap below the actual member size: the copy loop must abort, proving the
    # limit is enforced on drained bytes, not just declared metadata.
    zip_path = tmp_path / "export.zip"
    _write_zip(zip_path, {"apple_health_export/export.xml": XML_BODY})
    monkeypatch.setattr(apple_import, "_MAX_XML_BYTES", 10)
    with pytest.raises(AppleImportError, match="not a plausible"):
        open_export_xml(zip_path)


def test_open_missing_file_fails(tmp_path):
    with pytest.raises(AppleImportError, match="file not found"):
        open_export_xml(tmp_path / "nope.xml")


def test_open_non_xml_file_fails(tmp_path):
    junk = tmp_path / "data.xml"
    junk.write_bytes(b"definitely not xml")
    with pytest.raises(AppleImportError, match="does not look like"):
        open_export_xml(junk)


# --- import_apple_export -----------------------------------------------------


def _capture_transport(captured: dict, status_code: int = 200, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-Open-Wearables-API-Key")
        captured["content_type"] = request.headers.get("Content-Type", "")
        captured["body"] = request.read()
        return httpx.Response(
            status_code,
            json=body if body is not None else {"status": "processing", "task_id": "t-1"},
        )

    return httpx.MockTransport(handler)


def test_import_uploads_multipart_with_api_key(tmp_path, settings):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)
    captured: dict = {}

    result = import_apple_export(
        xml, settings, user_id="user-42", transport=_capture_transport(captured)
    )

    assert captured["url"] == (
        "http://open-wearables.test/api/v1/users/user-42/import/apple/xml/direct"
    )
    assert captured["api_key"] == "test-ow-api-key"
    assert captured["content_type"].startswith("multipart/form-data")
    assert XML_BODY in captured["body"]
    assert result.task_id == "t-1"
    assert result.user_id == "user-42"
    assert result.size_bytes == len(XML_BODY)


def test_import_streams_zip_member(tmp_path, settings):
    zip_path = tmp_path / "내보내기.zip"
    _write_zip(zip_path, {"apple_health_export/내보내기.xml": XML_BODY})
    captured: dict = {}

    result = import_apple_export(
        zip_path, settings, user_id="u", transport=_capture_transport(captured)
    )

    assert XML_BODY in captured["body"]
    assert result.filename == "내보내기.xml"


def test_import_requires_user_id(tmp_path, settings):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)
    # conftest settings has ow_user_id=None and no --user-id is given.
    with pytest.raises(AppleImportError, match="no open-wearables user id"):
        import_apple_export(xml, settings, transport=_capture_transport({}))


def test_import_falls_back_to_settings_user(tmp_path, settings):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)
    captured: dict = {}
    settings_with_user = settings.model_copy(update={"ow_user_id": "env-user"})

    result = import_apple_export(
        xml, settings_with_user, transport=_capture_transport(captured)
    )

    assert "/users/env-user/" in captured["url"]
    assert result.user_id == "env-user"


def test_import_surfaces_auth_failure(tmp_path, settings):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={}))
    with pytest.raises(AppleImportError, match="401"):
        import_apple_export(xml, settings, user_id="u", transport=transport)


def test_import_surfaces_server_error(tmp_path, settings):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, text="worker down")
    )
    with pytest.raises(AppleImportError, match="HTTP 500"):
        import_apple_export(xml, settings, user_id="u", transport=transport)


def test_import_wraps_network_error_without_leaking_key(tmp_path, settings):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(AppleImportError) as excinfo:
        import_apple_export(
            xml, settings, user_id="u", transport=httpx.MockTransport(explode)
        )
    assert "ConnectError" in str(excinfo.value)
    assert "test-ow-api-key" not in str(excinfo.value)


def test_import_rejects_non_json_success_body(tmp_path, settings):
    xml = tmp_path / "export.xml"
    xml.write_bytes(XML_BODY)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>proxy page</html>")
    )
    with pytest.raises(AppleImportError, match="non-JSON"):
        import_apple_export(xml, settings, user_id="u", transport=transport)
