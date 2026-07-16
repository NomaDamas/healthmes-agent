"""Upload an Apple Health export (export.xml or the export ZIP) to open-wearables.

Apple's Health app produces ``내보내기.zip``/``export.zip`` containing
``apple_health_export/export.xml`` (localized installs localize the inner
name too). open-wearables' ``POST /api/v1/users/{id}/import/apple/xml/direct``
accepts only the raw XML (the Celery task writes the bytes to a temp file and
parses them as XML — no archive handling), so this module extracts the main
export XML from a ZIP before uploading.

This is the zero-app-code ingestion path (docs/PLAN.md §13): Health app →
"모든 건강 데이터 보내기" → ``healthmes import apple <file>`` → real data in
the data plane. Continuous ingestion (bridge receiver) builds on top of it.
"""

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from healthmes.config import Settings

logger = logging.getLogger(__name__)

# CDA/ECG companions inside the export archive are not Health-record exports
# the importer understands; only the main export XML is uploaded.
_EXCLUDED_XML_MARKERS = ("cda", "ecg", "electrocardiogram")

# The export XML for years of data reaches hundreds of MB; refuse anything
# that obviously is not a Health export before buffering it into memory.
_MAX_XML_BYTES = 2 * 1024**3  # 2 GiB


class AppleImportError(Exception):
    """The export file could not be read, selected, or uploaded."""


@dataclass(frozen=True)
class AppleImportResult:
    """Outcome of a direct-import upload (open-wearables queues it async)."""

    task_id: str
    user_id: str
    filename: str
    size_bytes: int


def _pick_export_xml(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    """The main export XML inside an Apple Health ZIP.

    Localized exports rename ``export.xml`` (e.g. ``내보내기.xml``), so the
    selection is structural: the largest ``.xml`` member that is not a
    CDA/ECG companion file.
    """
    candidates = [
        info
        for info in archive.infolist()
        if info.filename.lower().endswith(".xml")
        and not info.is_dir()
        and not any(marker in Path(info.filename).name.lower() for marker in _EXCLUDED_XML_MARKERS)
    ]
    if not candidates:
        raise AppleImportError(
            "no export XML found inside the ZIP — expected an Apple Health "
            "export archive (contains apple_health_export/export.xml)"
        )
    return max(candidates, key=lambda info: info.file_size)


def load_export_xml(path: Path) -> tuple[str, bytes]:
    """Read ``path`` and return ``(upload_filename, xml_bytes)``.

    Accepts the raw ``export.xml`` or the ZIP straight from the Health app.
    """
    if not path.exists():
        raise AppleImportError(f"file not found: {path}")

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            member = _pick_export_xml(archive)
            if member.file_size > _MAX_XML_BYTES:
                raise AppleImportError(
                    f"export XML is {member.file_size} bytes (> {_MAX_XML_BYTES}); "
                    "not a plausible Apple Health export"
                )
            data = archive.read(member)
            return Path(member.filename).name, data

    if path.stat().st_size > _MAX_XML_BYTES:
        raise AppleImportError(
            f"{path} is {path.stat().st_size} bytes (> {_MAX_XML_BYTES}); "
            "not a plausible Apple Health export"
        )
    data = path.read_bytes()
    head = data.lstrip()[:200]
    if not head.startswith(b"<?xml") and b"<HealthData" not in head:
        raise AppleImportError(
            f"{path} does not look like an Apple Health export "
            "(expected XML, or pass the ZIP from the Health app)"
        )
    return path.name, data


def import_apple_export(
    path: Path,
    settings: Settings,
    *,
    user_id: str | None = None,
    timeout: float = 300.0,
    transport: httpx.BaseTransport | None = None,
) -> AppleImportResult:
    """Upload the export at ``path`` to open-wearables for ``user_id``.

    open-wearables answers 202-style ``{"status": "processing", "task_id":
    ...}`` immediately and parses in a background worker; large exports keep
    importing after this returns.
    """
    resolved_user = (user_id or settings.ow_user_id or "").strip()
    if not resolved_user:
        raise AppleImportError(
            "no open-wearables user id: pass --user-id or set HEALTHMES_OW_USER_ID"
        )
    api_key = settings.ow_api_key.get_secret_value()
    if not api_key:
        raise AppleImportError("open-wearables API key missing: set HEALTHMES_OW_API_KEY")

    filename, xml_bytes = load_export_xml(path)
    url = (
        f"{settings.ow_base_url.rstrip('/')}"
        f"/api/v1/users/{resolved_user}/import/apple/xml/direct"
    )
    logger.info("uploading %s (%d bytes) to %s", filename, len(xml_bytes), url)

    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(
            url,
            headers={"X-Open-Wearables-API-Key": api_key},
            files={"file": (filename, xml_bytes, "application/xml")},
        )
    if response.status_code == 401:
        raise AppleImportError("open-wearables rejected the API key (401)")
    if response.status_code >= 400:
        raise AppleImportError(
            f"upload failed: HTTP {response.status_code} — {response.text[:300]}"
        )

    body = response.json()
    return AppleImportResult(
        task_id=str(body.get("task_id", "")),
        user_id=resolved_user,
        filename=filename,
        size_bytes=len(xml_bytes),
    )
