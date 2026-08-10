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

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

MAX_BYTES = 5 * 1024 * 1024  # 5 MB


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

        extension = ALLOWED_CONTENT_TYPES.get((upload.content_type or "").lower())
        if extension is None:
            return Response(
                {"detail": "Unsupported image type. Use JPEG, PNG, WebP or GIF."},
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

        upload_dir = Path(settings.MEDIA_ROOT)
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Random suffix so two uploads of "photo.jpg" cannot overwrite each other.
        filename = f"{safe_stem(upload.name)}-{secrets.token_hex(8)}{extension}"

        # `.chunks()` streams the file instead of loading it whole.
        with open(upload_dir / filename, "wb") as destination:
            for chunk in upload.chunks():
                destination.write(chunk)

        return Response({"image_url": f"{settings.MEDIA_URL}{filename}"})
