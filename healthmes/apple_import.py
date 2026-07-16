"""Upload an Apple Health export (export.xml or the export ZIP) to open-wearables.

Apple's Health app produces ``내보내기.zip``/``export.zip`` containing
``apple_health_export/export.xml`` (localized installs localize the inner
name too). open-wearables' ``POST /api/v1/users/{id}/import/apple/xml/direct``
accepts only the raw XML (the Celery task writes the bytes to a temp file and
parses them as XML — no archive handling), so this module extracts the main
export XML from a ZIP before uploading.

Export XMLs for years of data reach hundreds of MB, so nothing is buffered
in memory: ZIP members are copied to a size-capped temp file and the upload
streams a file object through httpx multipart.

This is the zero-app-code ingestion path (docs/PLAN.md §13): Health app →
"모든 건강 데이터 보내기" → ``healthmes import apple <file>`` → real data in
the data plane. Continuous ingestion (bridge receiver) builds on top of it.
"""

import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx

from healthmes.config import Settings

logger = logging.getLogger(__name__)

# CDA/ECG companions inside the export archive are not Health-record exports
# the importer understands; only the main export XML is uploaded.
_EXCLUDED_XML_MARKERS = ("cda", "ecg", "electrocardiogram")

# Copy-time ceiling: declared ZIP sizes can lie (zip bomb), so the cap is
# enforced while draining the stream, not just against metadata.
_MAX_XML_BYTES = 2 * 1024**3  # 2 GiB
_COPY_CHUNK = 1024 * 1024


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


def _capped_copy(src: IO[bytes], dst: IO[bytes], *, limit: int) -> int:
    """Copy ``src`` into ``dst`` chunk-wise; error out past ``limit`` bytes."""
    total = 0
    while True:
        chunk = src.read(_COPY_CHUNK)
        if not chunk:
            return total
        total += len(chunk)
        if total > limit:
            raise AppleImportError(
                f"export XML exceeds {limit} bytes while extracting; "
                "not a plausible Apple Health export"
            )
        dst.write(chunk)


def open_export_xml(path: Path) -> tuple[str, IO[bytes], int]:
    """Open ``path`` and return ``(upload_filename, xml_stream, size_bytes)``.

    Accepts the raw ``export.xml`` or the ZIP straight from the Health app.
    ZIP members are extracted to an unlinked temp file (never in memory);
    the returned stream is positioned at 0 and the caller must close it.
    """
    if not path.exists():
        raise AppleImportError(f"file not found: {path}")

    try:
        is_zip = zipfile.is_zipfile(path)
    except OSError as exc:
        raise AppleImportError(f"cannot read {path}: {exc}") from exc

    if is_zip:
        spool = tempfile.TemporaryFile()
        try:
            with zipfile.ZipFile(path) as archive:
                member = _pick_export_xml(archive)
                if member.file_size > _MAX_XML_BYTES:
                    raise AppleImportError(
                        f"export XML is {member.file_size} bytes "
                        f"(> {_MAX_XML_BYTES}); not a plausible Apple Health export"
                    )
                with archive.open(member) as src:
                    size = _capped_copy(src, spool, limit=_MAX_XML_BYTES)
            spool.seek(0)
            return Path(member.filename).name, spool, size
        except (zipfile.BadZipFile, OSError) as exc:
            spool.close()
            raise AppleImportError(f"cannot extract {path}: {exc}") from exc
        except Exception:
            spool.close()
            raise

    try:
        size = path.stat().st_size
        if size > _MAX_XML_BYTES:
            raise AppleImportError(
                f"{path} is {size} bytes (> {_MAX_XML_BYTES}); "
                "not a plausible Apple Health export"
            )
        stream = path.open("rb")
    except OSError as exc:
        raise AppleImportError(f"cannot read {path}: {exc}") from exc

    try:
        head = stream.read(200).lstrip()
        if not head.startswith(b"<?xml") and b"<HealthData" not in head:
            raise AppleImportError(
                f"{path} does not look like an Apple Health export "
                "(expected XML, or pass the ZIP from the Health app)"
            )
        stream.seek(0)
    except AppleImportError:
        stream.close()
        raise
    except OSError as exc:
        stream.close()
        raise AppleImportError(f"cannot read {path}: {exc}") from exc
    return path.name, stream, size


def import_apple_export(
    path: Path,
    settings: Settings,
    *,
    user_id: str | None = None,
    timeout: float = 600.0,
    transport: httpx.BaseTransport | None = None,
) -> AppleImportResult:
    """Upload the export at ``path`` to open-wearables for ``user_id``.

    open-wearables answers 202-style ``{"status": "processing", "task_id":
    ...}`` immediately and parses in a background worker; large exports keep
    importing after this returns. The body is streamed from disk — memory
    stays flat regardless of export size.
    """
    resolved_user = (user_id or settings.ow_user_id or "").strip()
    if not resolved_user:
        raise AppleImportError(
            "no open-wearables user id: pass --user-id or set HEALTHMES_OW_USER_ID"
        )
    api_key = settings.ow_api_key.get_secret_value()
    if not api_key:
        raise AppleImportError("open-wearables API key missing: set HEALTHMES_OW_API_KEY")

    filename, xml_stream, size = open_export_xml(path)
    url = (
        f"{settings.ow_base_url.rstrip('/')}"
        f"/api/v1/users/{resolved_user}/import/apple/xml/direct"
    )
    logger.info("uploading %s (%d bytes) to %s", filename, size, url)

    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(
                url,
                headers={"X-Open-Wearables-API-Key": api_key},
                files={"file": (filename, xml_stream, "application/xml")},
            )
    except httpx.HTTPError as exc:
        # Never repr the request (headers carry the API key) — name + message only.
        raise AppleImportError(
            f"upload failed: {exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        xml_stream.close()

    if response.status_code == 401:
        raise AppleImportError("open-wearables rejected the API key (401)")
    if response.status_code >= 400:
        raise AppleImportError(
            f"upload failed: HTTP {response.status_code} — {response.text[:300]}"
        )

    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise AppleImportError(
            f"open-wearables returned HTTP {response.status_code} but a non-JSON "
            f"body: {response.text[:200]!r}"
        ) from exc
    return AppleImportResult(
        task_id=str(body.get("task_id", "")),
        user_id=resolved_user,
        filename=filename,
        size_bytes=size,
    )
