"""Upload an Apple Health export (export.xml or the export ZIP) to open-wearables.

Apple's Health app produces ``내보내기.zip``/``export.zip`` containing
``apple_health_export/export.xml`` (localized installs localize the inner
name too). open-wearables' ``POST /api/v1/users/{id}/import/apple/xml/direct``
accepts only the raw XML (the Celery task writes the bytes to a temp file and
parses them as XML — no archive handling), so this module extracts the main
export XML from a ZIP before uploading.

Nothing is buffered in client memory: both ZIP members and raw XML are
snapshotted through a size-capped copy into an unlinked temp file and the
upload streams that file object through httpx multipart. The cap defaults to
256 MiB because the vendor ``/direct`` endpoint holds the whole body in
server memory and hands the bytes to Celery (its docs steer larger files to
the S3 presigned flow, which needs AWS SNS and is not part of the local
stack). Making the vendor task take a file reference is a candidate upstream
PR under the PLAN §1 vendor policy.

This is the zero-app-code ingestion path (docs/PLAN.md §13): Health app →
"모든 건강 데이터 보내기" → ``healthmes import apple <file>`` → real data in
the data plane. Continuous ingestion (``/v1/ingest/healthkit``) builds on it.
"""

import json
import logging
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import httpx

from healthmes.config import Settings

logger = logging.getLogger(__name__)

# CDA/ECG companions inside the export archive are not Health-record exports
# the importer understands; only the main export XML is uploaded.
_EXCLUDED_XML_MARKERS = ("cda", "ecg", "electrocardiogram")

# Server-memory-bound ceiling (see module docstring), enforced while draining
# the stream — declared ZIP sizes can lie and files can grow mid-read.
DEFAULT_MAX_XML_BYTES = 256 * 1024 * 1024
_WARN_BYTES = 32 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024

# Everything the zipfile machinery raises for hostile/exotic archives
# (unsupported compression → NotImplementedError, CRC/password → RuntimeError).
_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    NotImplementedError,
    RuntimeError,
    OSError,
    EOFError,
)


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
                f"export XML exceeds {limit} bytes; the direct-import endpoint "
                "holds the whole file in server memory, so larger exports are "
                "refused — export a shorter date range from the Health app, "
                "or raise the cap explicitly with --max-bytes"
            )
        dst.write(chunk)


def open_export_xml(
    path: Path, *, max_bytes: int = DEFAULT_MAX_XML_BYTES
) -> tuple[str, IO[bytes], int]:
    """Open ``path`` and return ``(upload_filename, xml_stream, size_bytes)``.

    Accepts the raw ``export.xml`` or the ZIP straight from the Health app.
    Either way the bytes are snapshotted into an unlinked temp file through
    the capped copy (a growing/mutated source cannot exceed the cap or
    desync the upload length); the returned stream is positioned at 0 and
    the caller must close it.
    """
    if not path.exists():
        raise AppleImportError(f"file not found: {path}")

    try:
        is_zip = zipfile.is_zipfile(path)
    except OSError as exc:
        raise AppleImportError(f"cannot read {path}: {exc}") from exc

    spool = tempfile.TemporaryFile()
    try:
        if is_zip:
            with zipfile.ZipFile(path) as archive:
                member = _pick_export_xml(archive)
                if member.file_size > max_bytes:
                    raise AppleImportError(
                        f"export XML is {member.file_size} bytes (> {max_bytes}); "
                        "export a shorter range or raise --max-bytes"
                    )
                with archive.open(member) as src:
                    size = _capped_copy(src, spool, limit=max_bytes)
            filename = Path(member.filename).name
        else:
            with path.open("rb") as src:
                head = src.read(200).lstrip()
                if not head.startswith(b"<?xml") and b"<HealthData" not in head:
                    raise AppleImportError(
                        f"{path} does not look like an Apple Health export "
                        "(expected XML, or pass the ZIP from the Health app)"
                    )
                src.seek(0)
                size = _capped_copy(src, spool, limit=max_bytes)
            filename = path.name
    except AppleImportError:
        spool.close()
        raise
    except _ZIP_READ_ERRORS as exc:
        spool.close()
        raise AppleImportError(f"cannot extract {path}: {exc}") from exc
    except Exception:
        spool.close()
        raise

    spool.seek(0)
    return filename, spool, size


def _redact(text: str, secret: str) -> str:
    """Server-controlled text with the API key masked (echo/debug responses)."""
    return text.replace(secret, "***") if secret else text


def _discover_sole_user(
    settings: Settings, *, transport: httpx.BaseTransport | None
) -> str:
    """The open-wearables user id when exactly one user exists.

    Mirrors the repo convention that ``HEALTHMES_OW_USER_ID`` is optional on
    single-user installs: ambiguity is an error, never a guess.
    """
    api_key = settings.ow_api_key.get_secret_value()
    url = f"{settings.ow_base_url.rstrip('/')}/api/v1/users"
    try:
        with httpx.Client(timeout=30.0, transport=transport) as client:
            response = client.get(
                url,
                params={"limit": 2},
                headers={"X-Open-Wearables-API-Key": api_key},
            )
    except httpx.HTTPError as exc:
        raise AppleImportError(
            f"cannot discover the open-wearables user: {exc.__class__.__name__}: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise AppleImportError(
            f"cannot discover the open-wearables user: HTTP {response.status_code}"
        )
    try:
        items = response.json().get("items", [])
    except (json.JSONDecodeError, ValueError, AttributeError) as exc:
        raise AppleImportError(
            "cannot discover the open-wearables user: unexpected response shape"
        ) from exc
    if len(items) == 1 and items[0].get("id"):
        return str(items[0]["id"])
    if not items:
        raise AppleImportError(
            "open-wearables has no users yet — create one first "
            "(open-wearables dashboard), then re-run"
        )
    raise AppleImportError(
        "open-wearables has multiple users — pass --user-id or set "
        "HEALTHMES_OW_USER_ID"
    )


def import_apple_export(
    path: Path,
    settings: Settings,
    *,
    user_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_XML_BYTES,
    timeout: float = 600.0,
    transport: httpx.BaseTransport | None = None,
) -> AppleImportResult:
    """Upload the export at ``path`` to open-wearables for ``user_id``.

    open-wearables answers ``{"status": "processing", "task_id": ...}``
    immediately and parses in a background worker; large exports keep
    importing after this returns. The body is streamed from disk — client
    memory stays flat regardless of export size.
    """
    api_key = settings.ow_api_key.get_secret_value()
    if not api_key:
        raise AppleImportError("open-wearables API key missing: set HEALTHMES_OW_API_KEY")
    resolved_user = (user_id or settings.ow_user_id or "").strip()
    if not resolved_user:
        resolved_user = _discover_sole_user(settings, transport=transport)

    filename, xml_stream, size = open_export_xml(path, max_bytes=max_bytes)
    if size > _WARN_BYTES:
        logger.warning(
            "%s is %d MB; the vendor /direct endpoint buffers it in server "
            "memory — expect a slow import on the local stack",
            filename,
            size // (1024 * 1024),
        )
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
            f"upload failed: HTTP {response.status_code} — "
            f"{_redact(response.text[:300], api_key)}"
        )

    body: Any
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise AppleImportError(
            f"open-wearables returned HTTP {response.status_code} but a non-JSON "
            f"body: {_redact(response.text[:200], api_key)!r}"
        ) from exc
    task_id = body.get("task_id") if isinstance(body, dict) else None
    if not task_id:
        raise AppleImportError(
            "open-wearables did not acknowledge the import (no task_id in "
            f"response: {_redact(str(body)[:200], api_key)!r}) — treat the "
            "upload as NOT imported"
        )
    return AppleImportResult(
        task_id=str(task_id),
        user_id=resolved_user,
        filename=filename,
        size_bytes=size,
    )
