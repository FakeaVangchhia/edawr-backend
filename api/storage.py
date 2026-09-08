"""Where uploaded images actually live.

Two backends behind one pair of functions, chosen by `UPLOAD_BACKEND`:

    local   a directory on disk. Development, and what production was.
    r2      Cloudflare R2, over its S3-compatible API.

**The stored value is the same either way**: `image_url` on Product, Category
and OrderItem keeps holding the relative string `/uploads/<name>`, and the R2
object key is `uploads/<name>` — the same path without the leading slash. So
the two map onto each other one-to-one, no row has to be rewritten to move
between them, and the hostname still never reaches the database. Clients put
the host back with `assetUrl()`, exactly as before; the only change on their
side is *which* host, and that is one environment variable.

That relative path is also load-bearing for `OrderItem.image_url`, which is a
frozen snapshot taken at checkout — financial history that a storage migration
has no business rewriting.

WHY THIS EXISTS AT ALL
----------------------
Images used to be written to a Render persistent disk. A disk **pins the
service to one instance**: Render will not run two copies of a service that
mounts one, and attaching it disables zero-downtime deploys. `render.yaml` has
said since it was written that the day that stops being acceptable, this moves
to object storage and the disk goes. This is that.

WHAT `delete` PROMISES
----------------------
It never raises. A missing file is the desired end state, and failing a product
delete because its picture had already gone would be absurd. It also refuses
anything that is not a `/uploads/<name>` path this application produced — that
check is what stops a client-supplied `image_url` from becoming a delete on an
arbitrary path or an arbitrary object, and it matters more here than it did on
disk, because a bucket has no directory to be confined to.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("api")

# The bytes a browser needs to be told about, keyed by the extension the upload
# view's magic-byte sniff decided on. R2 stores whatever content type it is
# given and defaults to application/octet-stream — an image saved without this
# downloads instead of rendering, and nothing in the upload path reports it.
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Filenames carry `secrets.token_hex(8)`, so an object at a given key is never
# replaced — only orphaned and deleted. That makes an immutable, year-long
# cache correct rather than merely optimistic, and it is most of the reason for
# moving off a disk Django was serving single-threaded.
CACHE_CONTROL = "public, max-age=31536000, immutable"


class StorageError(RuntimeError):
    """The object store could not be reached, or refused the write.

    Raised only by `save`. The upload view turns it into a 502: unlike an audit
    row or a push notification, an upload that did not happen must be reported,
    because the shopkeeper is about to save a product that has no picture.
    """


def content_type_for(extension: str) -> str:
    return CONTENT_TYPES.get(extension, "application/octet-stream")


def _own_name(image_url: str | None) -> str | None:
    """The bare filename of an upload this application wrote, or None.

    Shared by both backends so they cannot disagree about what "ours" means.
    Anything absolute (a seeded placeholder on someone else's CDN), anything
    not under MEDIA_URL, and anything whose last segment is empty is not ours.
    `Path(...).name` also collapses `/uploads/../secrets` to `secrets`, which
    the callers then fail to find rather than acting on.
    """
    if not image_url or not image_url.startswith(settings.MEDIA_URL):
        return None
    name = Path(image_url[len(settings.MEDIA_URL):]).name
    return name or None


# --------------------------------------------------------------------------
# local disk
# --------------------------------------------------------------------------


def _local_save(filename: str, upload, content_type: str) -> str:
    upload_dir = Path(settings.MEDIA_ROOT)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # `.chunks()` streams the file instead of loading it whole.
    with open(upload_dir / filename, "wb") as destination:
        for chunk in upload.chunks():
            destination.write(chunk)

    return f"{settings.MEDIA_URL}{filename}"


def _local_delete(image_url: str | None) -> None:
    name = _own_name(image_url)
    if not name:
        return

    # Resolve and confirm the result is still directly inside MEDIA_ROOT. Belt
    # and braces with `_own_name` above: a symlink could still lead out.
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / name).resolve()
    if target.parent != root:
        return

    try:
        target.unlink(missing_ok=True)
    except OSError:
        # Locked by another process, or a permission problem on the mount. An
        # orphaned file is untidy; a failed request is a broken feature.
        logger.warning("could not delete upload", extra={"file": name})


# --------------------------------------------------------------------------
# Cloudflare R2
# --------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()


def r2_key(filename: str) -> str:
    """`uploads/<name>` — MEDIA_URL without its leading slash."""
    return f"{settings.MEDIA_URL.lstrip('/')}{filename}"


def client():
    """The S3 client, built once.

    boto3 clients are thread-safe and gunicorn runs eight threads on one
    worker, so building one per request would be pure waste. The lock is
    because those threads start together.

    Three pieces of configuration here are wrong by default against R2:

    - **`region_name="auto"`.** R2 has no regions; the signature still needs
      one, and "auto" is what Cloudflare documents.
    - **The checksum settings.** boto3 >= 1.36 attaches its own integrity
      headers to every request unless told to send them only where the
      operation requires it. R2's handling of the newer ones is inconsistent,
      and the failure is a rejected upload rather than a warning.
    - **Timeouts.** botocore's defaults are 60 seconds each. This runs inside a
      request a person is waiting on, so a store that has gone quiet must fail
      in seconds, not hang a gunicorn thread for a minute.
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client(
                "s3",
                endpoint_url=settings.R2_ENDPOINT_URL,
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                config=Config(
                    region_name="auto",
                    signature_version="s3v4",
                    s3={"addressing_style": "virtual"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    retries={"mode": "standard", "max_attempts": 3},
                    connect_timeout=5,
                    read_timeout=20,
                ),
            )
    return _client


def _r2_save(filename: str, upload, content_type: str) -> str:
    key = r2_key(filename)
    try:
        # upload_fileobj streams and switches to a multipart upload on its own.
        # put_object would need the whole body signed in one go.
        client().upload_fileobj(
            upload,
            settings.R2_BUCKET,
            key,
            ExtraArgs={"ContentType": content_type, "CacheControl": CACHE_CONTROL},
        )
    except Exception as exc:  # noqa: BLE001 — botocore raises a wide family
        logger.warning("upload to R2 failed", extra={"key": key, "error": str(exc)})
        raise StorageError(str(exc)) from exc

    return f"{settings.MEDIA_URL}{filename}"


def _r2_delete(image_url: str | None) -> None:
    name = _own_name(image_url)
    if not name:
        return

    try:
        client().delete_object(Bucket=settings.R2_BUCKET, Key=r2_key(name))
    except Exception:  # noqa: BLE001 — same contract as the local backend
        # Deleting an absent key is already a success in S3, so anything here is
        # a transport or credentials problem. An orphaned object costs a
        # fraction of a penny; a 500 on a product delete costs a phone call.
        logger.warning("could not delete upload from R2", extra={"file": name})


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------


def using_r2() -> bool:
    return settings.UPLOAD_BACKEND == "r2"


def save(filename: str, upload, content_type: str) -> str:
    """Store `upload` under `filename` and return the value to put in the row.

    Always `/uploads/<filename>`, whichever backend ran. Raises StorageError,
    and only StorageError, when the write did not happen.
    """
    if using_r2():
        return _r2_save(filename, upload, content_type)
    return _local_save(filename, upload, content_type)


def delete(image_url: str | None) -> None:
    """Remove an upload this application wrote, if it is still there.

    Never raises. See the module docstring.
    """
    if using_r2():
        _r2_delete(image_url)
    else:
        _local_delete(image_url)


def public_url(image_url: str | None) -> str | None:
    """The absolute URL a browser would fetch `image_url` from.

    Not used by any serializer — the clients still do this themselves, so the
    database keeps holding relative paths. It exists for management commands
    and for anything that needs to *check* an image is reachable.
    """
    if not image_url:
        return None
    if not image_url.startswith(settings.MEDIA_URL):
        return image_url
    base = settings.R2_PUBLIC_BASE_URL.rstrip("/") if using_r2() else ""
    return f"{base}{image_url}" if base else image_url
