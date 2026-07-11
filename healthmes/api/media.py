"""Media capture endpoints (issue #10 companion apps; docs/PLAN.md §8, §9).

The phone apps capture meals and medication/symptom photos or voice memos
natively. The flow is two-step:

1. ``POST /v1/media`` (multipart, field ``file``) stores the raw bytes under
   ``{data_dir}/media/YYYY/MM/<uuid><ext>`` and returns the ``media_path``
   token — the same data-dir-relative path convention every ``media_path``
   column in the store uses (healthmes/store/models.py). The Telegram capture
   path keeps writing its own files into the same tree.
2. The app passes that ``media_path`` string to ``POST /v1/food-logs`` or
   ``POST /v1/medical-records`` — only the path is ever stored in the
   database, never bytes.

``GET /v1/media/{media_path}`` serves the bytes back. It accepts the exact
token the upload returned (the leading ``media/`` segment may also be
dropped) and authenticates via the bearer token OR the derived read-only
viewer ``?token=`` credential (healthmes/api/auth.py) — decision/report pages
and in-app web views embed media through ``<img>``/``<audio>`` tags, which
cannot attach headers. Uploading stays bearer-only (the query credential
never authorizes non-GET requests).

Security rules (medical photos live here — docs/PLAN.md §9):

- Client filenames are never trusted, stored, or even read: the server
  generates a uuid filename and derives the extension from the validated
  content type.
- Uploads are capped at ``Settings.media_max_upload_bytes`` (413 beyond,
  partial file removed) and restricted to the §8 capture set — jpeg/png/
  heic/webp photos, m4a/mp3/ogg/wav voice notes (415 otherwise). The cap is
  enforced BEFORE the body is received, not just before it is persisted:
  multipart parsing spools file parts to the temp dir, so the handler owns
  body parsing itself (no ``UploadFile`` route parameter — FastAPI would
  parse/spool the whole body before any handler code runs) and requires a
  ``Content-Length`` no larger than cap + a small envelope allowance (411
  without one, 413 beyond it). The HTTP server (h11) never delivers more
  body bytes than the declared ``Content-Length``, so a lying client cannot
  spool past the cap either — that is what actually keeps a LAN peer from
  filling the disk.
- Serving resolves strictly under ``{data_dir}/media``: conservative
  per-segment charset, no dot segments or dotfiles, no backslashes, and a
  resolved-path containment check (symlink escapes included). Every rejected
  path is a uniform 404 — probes learn nothing about the filesystem.
"""

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Starlette's type, not fastapi's subclass: ``request.form()`` (parsed by the
# handler itself — see upload_media) yields starlette UploadFile instances.
from starlette.datastructures import UploadFile

from healthmes.api.common import utc_now
from healthmes.api.errors import APIError, not_found
from healthmes.config import Settings

__all__ = ["router", "CANONICAL_CONTENT_TYPES", "MEDIA_CACHE_CONTROL"]

router = APIRouter(prefix="/v1/media", tags=["media"])

# Canonical stored content type -> file extension. This is the §8 capture
# vocabulary (photo or voice note); nothing else belongs in the media tree.
CANONICAL_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/webp": ".webp",
    "audio/mp4": ".m4a",  # m4a voice memos (iOS AVAudioRecorder / Android MediaRecorder)
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",  # telegram-style voice notes
    "audio/wav": ".wav",
}

# Common client spellings normalised to a canonical type above.
CONTENT_TYPE_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/heif": "image/heic",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "application/ogg": "audio/ogg",
}

# Extension -> served content type. A superset of the upload set: files
# written by the Telegram capture path may carry alternative spellings.
_SERVE_CONTENT_TYPES: dict[str, str] = {
    extension: content_type for content_type, extension in CANONICAL_CONTENT_TYPES.items()
} | {".jpeg": "image/jpeg", ".oga": "audio/ogg"}

_FALLBACK_CONTENT_TYPE = "application/octet-stream"

# One path segment: conservative charset, and the first character must not be
# a dot — rejects ``.``, ``..`` and dotfiles in a single rule.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")

_READ_CHUNK_BYTES = 1024 * 1024

# Allowance on top of the file-byte cap for the multipart envelope (boundary
# lines + part headers — a few hundred bytes in practice). The declared
# Content-Length may exceed the cap by at most this much; the exact per-file
# cap is still enforced after parsing.
_MULTIPART_ENVELOPE_SLACK = 64 * 1024

# Stored files are immutable (uuid names, never rewritten), so embedded
# surfaces may cache — but only privately (medical photos, docs/PLAN.md §9).
MEDIA_CACHE_CONTROL = "private, max-age=86400, immutable"


class MediaUploadOut(BaseModel):
    """Response of ``POST /v1/media`` — the contract the capture flows pin."""

    media_path: str  # data-dir-relative token, e.g. "media/2026/07/<uuid>.jpg"
    content_type: str  # canonical stored type (client aliases normalised)
    bytes: int


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _media_root(settings: Settings) -> Path:
    return settings.data_dir / "media"


def _canonical_content_type(raw: str | None) -> str:
    """Validate the declared content type against the capture allowlist."""
    declared = (raw or "").split(";", 1)[0].strip().lower()
    canonical = CONTENT_TYPE_ALIASES.get(declared, declared)
    if canonical not in CANONICAL_CONTENT_TYPES:
        raise APIError(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            f"content type {declared or '(none)'!r} is not accepted for media uploads",
            detail={"allowed": sorted(CANONICAL_CONTENT_TYPES)},
        )
    return canonical


def _payload_too_large(cap: int) -> APIError:
    return APIError(
        status.HTTP_413_CONTENT_TOO_LARGE,
        "payload_too_large",
        f"media upload exceeds the {cap}-byte cap",
        detail={"max_bytes": cap},
    )


# No ``UploadFile`` route parameter on purpose: FastAPI parses the multipart
# body (spooling file parts to the temp dir) BEFORE any handler or dependency
# code runs, which would let an oversized body hit the disk ahead of every
# cap check. The handler parses the form itself, after the size gate below;
# ``openapi_extra`` keeps the request body documented in the schema.
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": "The captured photo or voice note.",
                            }
                        },
                    }
                }
            },
        }
    },
)
async def upload_media(request: Request) -> MediaUploadOut:
    """Store one captured media file and return its ``media_path`` token.

    The token goes into ``POST /v1/food-logs`` / ``POST /v1/medical-records``
    (``media_path`` field) and back into ``GET /v1/media/{media_path}``.
    """
    settings = _settings(request)
    cap = settings.media_max_upload_bytes
    budget = cap + _MULTIPART_ENVELOPE_SLACK

    # Size gate BEFORE a single body byte is read. A Content-Length is
    # required (both companion apps send fixed-length bodies); h11 under
    # uvicorn never delivers more body bytes than declared, so multipart
    # parsing below can spool at most ``budget`` bytes to the temp dir no
    # matter what the client actually streams.
    declared_raw = request.headers.get("content-length")
    if declared_raw is None:
        raise APIError(
            status.HTTP_411_LENGTH_REQUIRED,
            "length_required",
            "media uploads must declare a Content-Length "
            "(chunked transfer encoding is not accepted)",
        )
    try:
        declared = int(declared_raw)
    except ValueError:  # h11 rejects malformed values first; belt and braces
        raise APIError(
            status.HTTP_411_LENGTH_REQUIRED, "length_required", "unparseable Content-Length"
        ) from None
    if declared > budget:
        raise _payload_too_large(cap)

    async with request.form() as form:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "missing_file",
                "multipart field 'file' with the captured bytes is required",
            )
        content_type = _canonical_content_type(upload.content_type)

        now = utc_now()
        directory = _media_root(settings) / f"{now:%Y}" / f"{now:%m}"
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{CANONICAL_CONTENT_TYPES[content_type]}"
        destination = directory / filename

        # Exclusive create before the cleanup guard: if the fresh uuid name
        # somehow exists, fail loudly WITHOUT unlinking the stranger's file.
        out = destination.open("xb")
        written = 0
        try:
            with out:
                # Exact per-file cap (the header gate above allows the small
                # multipart envelope on top of it): stream in chunks and 413
                # after at most one extra chunk.
                while chunk := await upload.read(_READ_CHUNK_BYTES):
                    written += len(chunk)
                    if written > cap:
                        raise _payload_too_large(cap)
                    out.write(chunk)
        except BaseException:
            destination.unlink(missing_ok=True)  # never leave partial files behind
            raise
        if written == 0:
            destination.unlink(missing_ok=True)
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "empty_file", "uploaded media file is empty"
            )

    # POSIX separators by construction — the token is a URL/DB value, not an
    # OS path (healthmes/store/models.py media_path convention).
    media_path = f"media/{now:%Y}/{now:%m}/{filename}"
    return MediaUploadOut(media_path=media_path, content_type=content_type, bytes=written)


def _resolve_media_file(settings: Settings, media_path: str) -> Path | None:
    """Strictly resolve ``media_path`` under ``{data_dir}/media`` (else None).

    Accepts the upload token (with its leading ``media/`` segment) or the
    path relative to the media root. All rejections collapse to ``None``
    (served as a uniform 404): backslashes, NULs, absolute paths, empty/dot
    segments, dotfiles, odd charsets, and resolved paths escaping the root
    (symlinks included).
    """
    if "\\" in media_path or "\x00" in media_path:
        return None
    parts = media_path.split("/")
    if parts and parts[0] == "media":
        parts = parts[1:]
    if not parts or not all(_SEGMENT_RE.fullmatch(part) for part in parts):
        return None
    root = _media_root(settings).resolve()
    candidate = root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


@router.get("/{media_path:path}")
def get_media(media_path: str, request: Request) -> FileResponse:
    """Serve a stored media file (bearer or viewer ``?token=`` — see module doc)."""
    file_path = _resolve_media_file(_settings(request), media_path)
    if file_path is None:
        raise not_found("media", media_path)
    return FileResponse(
        file_path,
        media_type=_SERVE_CONTENT_TYPES.get(file_path.suffix.lower(), _FALLBACK_CONTENT_TYPE),
        headers={"Cache-Control": MEDIA_CACHE_CONTROL},
    )
