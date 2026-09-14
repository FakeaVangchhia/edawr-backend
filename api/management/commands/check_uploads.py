"""Prove that the images the database points at actually exist and can be read.

    uv run manage.py check_uploads
    uv run manage.py check_uploads --public      # also fetch each one over HTTP

Every product image is two separate promises, and each can fail on its own
while the other looks fine:

    1. the bytes reached the configured store — the R2 bucket, or the disk;
    2. a browser can read them back from R2_PUBLIC_BASE_URL.

Neither is checked anywhere else, and both fail quietly. An upload written to a
container filesystem returns 200 and disappears on the next deploy. An upload
written correctly to R2 still 404s in every customer's browser until public
access is switched on for the bucket, because an R2 bucket is private until an
r2.dev subdomain or a custom domain is connected — and nothing on this side
depends on that URL, so nothing on this side notices.

This command is the answer to "did that upload actually land?", and it exits
non-zero when it did not, so it can go in a deploy check or `/preflight`.

It walks the **rows**, not the bucket or the directory, for the reason
`migrate_uploads_to_r2` does: a store accumulates orphaned objects that no
product points at, and those are not a fault. A row pointing at nothing is.

`--public` is off by default because it makes one HTTP request per distinct
image against a third party. Turn it on after changing R2_PUBLIC_BASE_URL,
after connecting a custom domain, and the first time uploads are switched to
R2 — those are the three moments this catches something.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from api import storage
from api.models import Category, OrderItem, Product, Promo


class Command(BaseCommand):
    help = "Verify every image_url in the database resolves in the configured store."

    def add_arguments(self, parser):
        parser.add_argument(
            "--public",
            action="store_true",
            help=(
                "Also fetch each image over HTTP from R2_PUBLIC_BASE_URL, which "
                "is what proves the bucket is actually readable by a browser."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Check at most this many images (0 = all). Useful as a smoke test.",
        )

    def handle(self, *args, **options):
        check_public = options["public"]
        limit = options["limit"]

        self.stdout.write(f"UPLOAD_BACKEND     {settings.UPLOAD_BACKEND}")
        if storage.using_r2():
            self.stdout.write(f"R2_BUCKET          {settings.R2_BUCKET or '(unset)'}")
            self.stdout.write(
                f"R2_PUBLIC_BASE_URL {settings.R2_PUBLIC_BASE_URL or '(unset)'}"
            )
            if not settings.R2_PUBLIC_BASE_URL:
                self.stderr.write(
                    self.style.WARNING(
                        "R2_PUBLIC_BASE_URL is unset. Uploads will succeed and "
                        "every image will 404 in the browser: an R2 bucket is "
                        "private until its r2.dev subdomain or a custom domain "
                        "is turned on. The clients need the same value as "
                        "NEXT_PUBLIC_MEDIA_URL / EXPO_PUBLIC_MEDIA_URL."
                    )
                )
        else:
            self.stdout.write(f"MEDIA_ROOT         {settings.MEDIA_ROOT}")
            self.stdout.write(
                f"disk declared persistent: "
                f"{'yes' if settings.UPLOAD_DISK_PERSISTENT else 'NO'}"
            )

        if check_public and not storage.using_r2():
            # There is no configured public host for the local backend — the
            # clients fall back to the API's own origin, which this process
            # cannot know and may not be able to reach from where it runs.
            raise CommandError(
                "--public only applies to UPLOAD_BACKEND=r2. With the local "
                "backend the images are served by whatever sits in front of "
                "MEDIA_ROOT; fetch one yourself to test that."
            )

        if check_public and not settings.R2_PUBLIC_BASE_URL:
            raise CommandError(
                "--public needs R2_PUBLIC_BASE_URL. That is the value being tested."
            )

        names = self._referenced_names()
        if not names:
            self.stdout.write("\nNo rows reference an uploaded image.")
            return

        if limit:
            names = names[:limit]

        self.stdout.write(f"\nChecking {len(names)} referenced image(s):\n")

        stored_missing: list[str] = []
        public_missing: list[tuple[str, str]] = []

        for name in names:
            if not self._in_store(name):
                stored_missing.append(name)
                self.stderr.write(f"  MISSING in store   {name}")
                continue

            if not check_public:
                self.stdout.write(f"  ok                 {name}")
                continue

            problem = self._fetch_publicly(name)
            if problem:
                public_missing.append((name, problem))
                self.stderr.write(f"  MISSING publicly   {name}  ({problem})")
            else:
                self.stdout.write(f"  ok, and readable   {name}")

        self._report(len(names), stored_missing, public_missing, check_public)

    # -- gathering ---------------------------------------------------------

    def _referenced_names(self) -> list[str]:
        """Distinct bare filenames referenced by any row, sorted.

        `Path(...).name` for the same reason api/storage.py uses it: a
        hand-edited row could hold "/uploads/../something", and this must not
        turn that into a request for it.
        """
        prefix = settings.MEDIA_URL
        names: set[str] = set()
        for model in (Product, Category, OrderItem, Promo):
            for value in (
                model.objects.filter(image_url__startswith=prefix)
                .values_list("image_url", flat=True)
                .distinct()
            ):
                bare = Path(value[len(prefix):]).name
                if bare:
                    names.add(bare)
        return sorted(names)

    # -- the two checks ----------------------------------------------------

    def _in_store(self, name: str) -> bool:
        if not storage.using_r2():
            return (Path(settings.MEDIA_ROOT) / name).is_file()
        try:
            storage.client().head_object(
                Bucket=settings.R2_BUCKET, Key=storage.r2_key(name)
            )
        except Exception:  # noqa: BLE001 — a 404 is the expected failure
            return False
        return True

    def _fetch_publicly(self, name: str) -> str | None:
        """None if a browser could read it, else a short reason.

        A HEAD, because the point is whether the object is reachable and not
        what is in it. `Content-Type` is checked too: R2 defaults to
        application/octet-stream, and an image stored with that downloads
        instead of rendering — a failure that looks like a broken image and is
        invisible in every log.
        """
        url = storage.public_url(f"{settings.MEDIA_URL}{name}")
        # A browser User-Agent, because the question this command asks is
        # "could a customer's browser load this?" and the default
        # `Python-urllib/3.x` is not a browser. Cloudflare's bot protection
        # sits in front of an r2.dev subdomain and answers that signature with
        # **403 and body `error code: 1010`** — indistinguishable here from a
        # private bucket, so without this the command reports every image as
        # unreachable on a bucket that is serving them perfectly well. That is
        # the worst possible failure for a verifier: it sends you to re-check
        # the one setting that was already right.
        request = urllib.request.Request(
            url,
            method="HEAD",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/png,*/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # Still hedged: a custom domain behind a WAF rule, or R2 token
                # auth, can also produce these. "Not enabled" is the usual
                # cause and the first thing to check, not the only one.
                return (
                    f"HTTP {exc.code} — public access is probably not enabled "
                    f"(or a WAF/bot rule is blocking non-browser clients)"
                )
            return f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 — URL, DNS and TLS all land here
            return exc.__class__.__name__

        if not content_type.startswith("image/"):
            return f"served as {content_type or 'nothing'}, not an image"
        return None

    # -- reporting ---------------------------------------------------------

    def _report(self, total, stored_missing, public_missing, check_public) -> None:
        if not stored_missing and not public_missing:
            suffix = " and readable over HTTP" if check_public else ""
            self.stdout.write(
                self.style.SUCCESS(f"\nAll {total} image(s) present{suffix}.")
            )
            return

        self.stdout.write("")
        if stored_missing:
            where = "the R2 bucket" if storage.using_r2() else str(settings.MEDIA_ROOT)
            self.stderr.write(
                self.style.ERROR(
                    f"{len(stored_missing)} of {total} image(s) are not in {where}."
                )
            )
            if storage.using_r2():
                self.stderr.write(
                    "  Either they predate the switch to R2 — copy them with "
                    "`manage.py migrate_uploads_to_r2`, on the instance that "
                    "mounts the disk — or they were written to a filesystem a "
                    "deploy has since discarded, in which case the bytes are "
                    "gone and the images have to be uploaded again."
                )
            else:
                self.stderr.write(
                    "  If this host has no persistent disk, that is where the "
                    "images went: set UPLOAD_BACKEND=r2 before uploading more."
                )

        if public_missing:
            self.stderr.write(
                self.style.ERROR(
                    f"\n{len(public_missing)} image(s) are in the store but a "
                    "browser cannot read them."
                )
            )
            self.stderr.write(
                "  Check R2_PUBLIC_BASE_URL against the bucket's Public access "
                "settings in the Cloudflare dashboard, and check that the two "
                "web clients were rebuilt with the same value in "
                "NEXT_PUBLIC_MEDIA_URL — it is baked in at build time."
            )

        raise CommandError("check_uploads found images the clients cannot display.")
