"""Product image upload.

Files land on local disk under `backend/uploads/` (settings.MEDIA_ROOT) and are
served back by the `/uploads/<path>` route in `config/urls.py`.

The response is `{"image_url": "/uploads/<name>"}` — a *relative* path on
purpose. The frontend runs it through `assetUrl()`, which prefixes
NEXT_PUBLIC_API_URL, so the same stored value works whether the API is on
localhost:8000 or a deployed host. Storing an absolute URL would bake the
hostname into the database.

**This is the one endpoint whose body is not JSON.** In FastAPI that meant a
special parameter type (`file: UploadFile = File(...)`) and an extra package
(`python-multipart`). In DRF, multipart is parsed by default: the uploaded file
is simply in `request.FILES`, and the key is the name the frontend used in its
`FormData` — `file`.
"""

import logging
import re
import secrets
from pathlib import Path

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from api.permissions import AdminAPIView
from api.serializers import UploadResponseSerializer

logger = logging.getLogger("api")

# What the file actually *is*, decided by reading it rather than by believing
# the client. `upload.content_type` is a header the browser writes and anyone
# can set: declaring an SVG as `image/png` used to get it stored as `.png` and
# served back with `Content-Type: image/png`, and an SVG is a document that can
# carry script. `SECURE_CONTENT_TYPE_NOSNIFF` blunts that, but "the file is what
# its first bytes say it is" is the check that actually settles it.
#
# Each entry is (prefix, offset, extension). Offsets are zero except WebP, whose
# marker sits after the RIFF length field.
MAGIC_SIGNATURES = [
    (bytes.fromhex("ffd8ff"), 0, ".jpg"),           # JPEG: SOI marker
    (bytes.fromhex("89504e470d0a1a0a"), 0, ".png"),  # PNG: 8-byte signature
    (b"GIF87a", 0, ".gif"),
    (b"GIF89a", 0, ".gif"),
    (b"WEBP", 8, ".webp"),                          # inside a RIFF container
]

# Enough bytes to cover the longest signature plus its offset.
MAGIC_PREFIX_BYTES = 16

MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def sniff_extension(head: bytes) -> str | None:
    """The extension implied by a file's first bytes, or None if it is not an image."""
    for prefix, offset, extension in MAGIC_SIGNATURES:
        if head[offset : offset + len(prefix)] == prefix:
            # A WebP file is a RIFF container; check the outer marker too, or
            # any RIFF (a .wav, say) with "WEBP" at byte 8 would pass.
            if extension == ".webp" and head[0:4] != b"RIFF":
                continue
            return extension
    return None


def delete_stored_image(image_url: str | None) -> None:
    """Remove an uploaded file this application wrote, if it still exists.

    Called when a product's image is replaced or its row deleted. Without it
    every image ever uploaded stayed on disk forever — the working tree had
    around 250 orphans before this existed, and on the production deployment
    that is a mounted bucket nobody is emptying.

    Deliberately forgiving. It refuses anything that is not a `/uploads/<name>`
    path this app produced, resolves the result and checks it is still inside
    MEDIA_ROOT, and never raises: a missing file is the desired end state, and
    failing a product delete because its picture had already gone would be
    absurd.
    """
    if not image_url or not image_url.startswith(settings.MEDIA_URL):
        # An externally hosted image, or a seeded placeholder. Not ours to
        # delete, and the check is what stops `image_url` becoming a path
        # traversal with a delete on the end of it.
        return

    name = Path(image_url[len(settings.MEDIA_URL):]).name
    if not name:
        return

    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / name).resolve()
    if target.parent != root:
        return

    try:
        target.unlink(missing_ok=True)
    except OSError:
        # Locked by another process, or a permission problem on the mount.
        # An orphaned file is untidy; a failed request is a broken feature.
        logger.warning("could not delete upload", extra={"file": name})


def safe_stem(filename: str) -> str:
    """Reduce a user-supplied filename to something harmless.

    Strips directory components and anything that is not alphanumeric, dash or
    underscore, so a name like `../../etc/passwd` cannot escape the upload dir.
    """
    stem = Path(filename or "image").stem
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return (cleaned or "image")[:40]


class ProductImageUploadView(AdminAPIView):
    # Declared explicitly even though MultiPartParser is in DRF's defaults —
    # naming the parsers is what tells a reader (and drf-spectacular) that this
    # endpoint takes a file rather than JSON.
    parser_classes = [MultiPartParser, FormParser]

    # There is no serializer to point at, so the request body is described as a
    # raw OpenAPI schema fragment. `format: binary` is what makes the Swagger UI
    # render a file picker.
    @extend_schema(
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
                "required": ["file"],
            }
        },
        responses=UploadResponseSerializer,
    )
    def post(self, request):
        """POST /api/uploads/products/image"""
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"detail": "No file was uploaded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Django reports the size before you read a byte, so the limit is
        # checked without buffering the file into this process. (Django has
        # already spooled anything over FILE_UPLOAD_MAX_MEMORY_SIZE to a temp
        # file, which it deletes when the request ends.)
        if upload.size > MAX_BYTES:
            return Response(
                {"detail": "Image is larger than 5 MB."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if upload.size == 0:
            return Response(
                {"detail": "No file was uploaded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Read the first bytes and let the file say what it is. This has to
        # happen after the size check (so a huge body is refused before it is
        # touched) and before anything is written.
        head = upload.read(MAGIC_PREFIX_BYTES)
        upload.seek(0)
        extension = sniff_extension(head)
        if extension is None:
            return Response(
                {"detail": "Unsupported image type. Use JPEG, PNG, WebP or GIF."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        upload_dir = Path(settings.MEDIA_ROOT)
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Random suffix so two uploads of "photo.jpg" cannot overwrite each other.
        filename = f"{safe_stem(upload.name)}-{secrets.token_hex(8)}{extension}"

        # `.chunks()` streams the file instead of loading it whole.
        with open(upload_dir / filename, "wb") as destination:
            for chunk in upload.chunks():
                destination.write(chunk)

        return Response({"image_url": f"{settings.MEDIA_URL}{filename}"})
