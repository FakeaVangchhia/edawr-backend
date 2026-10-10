"""Copy the images already on disk into the R2 bucket.

    uv run manage.py migrate_uploads_to_r2 --dry-run
    uv run manage.py migrate_uploads_to_r2

**It writes no database rows, and cannot.** `image_url` holds the relative path
`/uploads/<name>`, and the R2 object key is that same path without its leading
slash — so every row is already correct for both backends and the migration is
a file copy and nothing else. That is the whole reason `api/storage.py` kept the
stored value relative; an absolute URL in the column would have made this a
schema-wide rewrite touching frozen order history.

It walks the **rows**, not the directory. Those are different sets: a working
tree accumulates uploads that no product ever pointed at (replaced images,
abandoned drafts, and — until it was fixed — a test suite that wrote into the
real upload directory). Copying the directory would move all of that into a
bucket nobody will ever clean.

WHERE TO RUN IT
---------------
On Render, in the **web service's** shell. That is the only process with the
disk mounted: `render.yaml` gives the cron job no UPLOAD_DIR on purpose, and the
pre-deploy instance that runs `migrate` has no disk at all.

It is safe to re-run. Objects already in the bucket are left alone, so an
interrupted copy is resumed by running it again.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from api import storage
from api.views.uploads import MAGIC_PREFIX_BYTES, sniff_extension


class Command(BaseCommand):
    help = "Copy locally stored product and category images into the R2 bucket."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be copied without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Checked here rather than trusted: the command reads R2 settings
        # directly, so running it against a half-filled environment would
        # otherwise fail one object at a time.
        for name in ("R2_ENDPOINT_URL", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            if not getattr(settings, name):
                raise CommandError(
                    f"{name} is unset. This command talks to R2 directly and "
                    "needs the full set, whatever UPLOAD_BACKEND happens to be."
                )

        names = storage.referenced_names()
        if not names:
            self.stdout.write("No locally stored images are referenced by any row.")
            return

        root = Path(settings.MEDIA_ROOT)
        client = storage.client()
        bucket = settings.R2_BUCKET

        copied = skipped = missing = 0

        for name in sorted(names):
            key = storage.r2_key(name)
            source = root / name

            # The bucket first, then the disk. An image copied from another
            # machine is in the bucket and not on this one, and that is the
            # normal state of every image once the migration has run anywhere
            # — reporting it as "missing" would say the rows point at nothing
            # when they point at exactly the right place.
            try:
                client.head_object(Bucket=bucket, Key=key)
            except Exception:  # noqa: BLE001 — a 404 is the expected path here
                pass
            else:
                skipped += 1
                continue

            if not source.is_file():
                # Referenced by a row, in neither place. Usually means the
                # command is running somewhere without the disk mounted.
                self.stderr.write(f"  missing locally  {name}")
                missing += 1
                continue

            if dry_run:
                self.stdout.write(f"  would copy       {name}")
                copied += 1
                continue

            # Re-sniff rather than trusting the extension in the filename. The
            # content type is what decides whether a browser renders the image
            # or downloads it, and these files were written by an older version
            # of the upload view.
            with source.open("rb") as handle:
                extension = sniff_extension(handle.read(MAGIC_PREFIX_BYTES)) or source.suffix
                handle.seek(0)
                client.upload_fileobj(
                    handle,
                    bucket,
                    key,
                    ExtraArgs={
                        "ContentType": storage.content_type_for(extension),
                        "CacheControl": storage.CACHE_CONTROL,
                    },
                )
            self.stdout.write(f"  copied           {name}")
            copied += 1

        verb = "would copy" if dry_run else "copied"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{len(names)} referenced images: {verb} {copied}, "
                f"{skipped} already in the bucket, {missing} missing locally."
            )
        )
        if missing and not dry_run:
            self.stdout.write(
                "Missing files leave their rows pointing at nothing. Check "
                "whether this ran on the instance that mounts the disk."
            )
