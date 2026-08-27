"""Dump the database to a file, and keep the last N dumps.

    uv run manage.py backup_database --dry-run   # say what it would do
    uv run manage.py backup_database             # do it
    uv run manage.py backup_database --keep 30

**Order history is the business record of a cash shop.** It is what answers "did
this customer pay", "what did we sell last month", and "what does this rider owe
the till" — and until this command existed it lived in exactly one place, inside
Neon's free-tier restore window, which is a few days and is not a backup
strategy. `deployment.md` names this the highest-value missing piece and
it was right: everything else on that list costs a bad day, and this one costs
the business.

## Three decisions worth knowing about

**`pg_dump -Fc`, not `dumpdata`.** Django's own `dumpdata` needs no extra binary
and would have avoided touching the Dockerfile, but it serialises rows through
the ORM: it carries no schema, no indexes and no constraints, it is far slower
and larger, and restoring it needs a database whose migrations already match.
A `pg_dump` custom-format archive is compressed, restores with one `pg_restore`
into an empty database, and is what anyone recovering from a disaster at 2am
already knows how to use. A backup format nobody can restore under pressure is
not a backup.

**The dump directory must not live under `MEDIA_ROOT`.** In production
`SERVE_MEDIA=true` — a deliberate exception explained in `deployment.md` —
so Django serves everything beneath `/app/uploads` to anyone who asks. A dump
written there would be a downloadable copy of every customer's name, phone
number and address, reachable over plain HTTP by guessing a filename. The check
below refuses to run rather than trusting the operator to have noticed, because
this is exactly the mistake that looks fine until it is catastrophic.

**Nothing is pruned until the new dump is verified.** A `pg_dump` that fails
half way still leaves a file behind, so "the file exists" is not evidence. The
command checks the exit status *and* that the archive is plausibly sized *and*
that `pg_restore --list` can read it, and only then deletes anything. A rotation
that trims yesterday's good backup to make room for today's broken one is worse
than having no rotation at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# A custom-format archive of an empty schema is still a few kilobytes of catalog.
# Anything smaller than this did not finish.
MIN_PLAUSIBLE_BYTES = 1024


class Command(BaseCommand):
    help = "Write a pg_dump archive of the database and rotate old ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen and write nothing.",
        )
        parser.add_argument(
            "--keep",
            type=int,
            default=None,
            help="How many archives to retain (default: BACKUP_KEEP).",
        )
        parser.add_argument(
            "--dir",
            default=None,
            help="Where to write (default: BACKUP_DIR).",
        )

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        target = self.resolve_directory(options["dir"])
        keep = options["keep"] if options["keep"] is not None else settings.BACKUP_KEEP

        if "postgresql" not in database["ENGINE"]:
            raise CommandError(
                "This command dumps PostgreSQL. DATABASE_URL currently points at "
                f"{database['ENGINE']}, which production must never be."
            )
        if keep < 1:
            raise CommandError("--keep must be at least 1.")

        binary = shutil.which("pg_dump")
        if binary is None:
            raise CommandError(
                "pg_dump is not on PATH. Install the PostgreSQL client tools "
                "whose major version is at least the server's — an older "
                "pg_dump refuses to dump a newer server. The container image "
                "installs postgresql-client; see the Dockerfile."
            )

        # UTC and sortable, so `ls` is chronological and two dumps a second
        # apart cannot collide.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = target / f"edawr-{stamp}.dump"

        if options["dry_run"]:
            self.stdout.write(f"Would write   {archive}")
            self.stdout.write(f"Would keep    {keep} most recent")
            for stale in self.stale_archives(target, keep, incoming=1):
                self.stdout.write(f"Would delete  {stale.name}")
            return

        target.mkdir(parents=True, exist_ok=True)
        self.dump(binary, database, archive)
        self.verify(archive)

        size_mb = archive.stat().st_size / 1_048_576
        self.stdout.write(self.style.SUCCESS(f"Wrote {archive} ({size_mb:.1f} MB)"))

        for stale in self.stale_archives(target, keep):
            stale.unlink()
            self.stdout.write(f"Removed {stale.name}")

    # --- pieces ----------------------------------------------------------
    def resolve_directory(self, override: str | None) -> Path:
        target = Path(override or settings.BACKUP_DIR)
        if not target.is_absolute():
            target = Path(settings.BASE_DIR) / target
        target = target.resolve()

        media = Path(settings.MEDIA_ROOT).resolve()
        if target == media or media in target.parents:
            raise CommandError(
                f"BACKUP_DIR ({target}) is inside MEDIA_ROOT ({media}).\n"
                "With SERVE_MEDIA=true — which production uses — Django serves "
                "everything under MEDIA_ROOT to anyone who asks, so the dump "
                "would be a public download of every customer's name, phone "
                "number and address. Point BACKUP_DIR somewhere else."
            )
        return target

    def dump(self, binary: str, database: dict, archive: Path) -> None:
        """Run pg_dump, with the password in the environment rather than argv.

        `pg_dump "postgres://user:password@host/db"` works and is one line
        shorter. It also puts the production database password in this process's
        command line, where `ps` shows it to every other process on the box and
        where a crash reporter would attach it to a bug report. `PGPASSWORD` is
        the documented way round that.
        """
        command = [
            binary,
            "--format=custom",   # compressed, and restorable selectively
            "--no-owner",        # a restore into a differently-named role works
            "--no-privileges",
            f"--host={database.get('HOST') or 'localhost'}",
            f"--port={database.get('PORT') or 5432}",
            f"--username={database.get('USER') or ''}",
            f"--dbname={database.get('NAME') or ''}",
            f"--file={archive}",
        ]

        environment = {**os.environ}
        if database.get("PASSWORD"):
            environment["PGPASSWORD"] = database["PASSWORD"]

        result = subprocess.run(
            command, env=environment, capture_output=True, text=True
        )
        if result.returncode != 0:
            # Remove the stub pg_dump leaves behind on failure, so a later run
            # cannot mistake it for a real archive and rotate a good one out.
            archive.unlink(missing_ok=True)
            raise CommandError(
                f"pg_dump exited {result.returncode}:\n{result.stderr.strip()}"
            )

    def verify(self, archive: Path) -> None:
        """Prove the archive is readable before anything is deleted for it."""
        if not archive.exists() or archive.stat().st_size < MIN_PLAUSIBLE_BYTES:
            archive.unlink(missing_ok=True)
            raise CommandError(
                "pg_dump reported success but wrote no usable archive. "
                "Nothing was rotated."
            )

        restore = shutil.which("pg_restore")
        if restore is None:  # pragma: no cover — ships alongside pg_dump
            return

        # `--list` reads the archive's table of contents without touching a
        # database. It is the cheapest thing that distinguishes "a file exists"
        # from "a file pg_restore can actually read", which is the distinction
        # that matters at 2am.
        result = subprocess.run(
            [restore, "--list", str(archive)], capture_output=True, text=True
        )
        if result.returncode != 0:
            archive.unlink(missing_ok=True)
            raise CommandError(
                f"The archive is not readable by pg_restore:\n{result.stderr.strip()}\n"
                "Nothing was rotated."
            )

    @staticmethod
    def stale_archives(target: Path, keep: int, incoming: int = 0) -> list[Path]:
        """Archives past the retention count, oldest first.

        `incoming` lets `--dry-run` account for the dump it would have written
        but did not, so the dry run reports the same deletions the real run does.
        """
        if not target.exists():
            return []
        archives = sorted(target.glob("edawr-*.dump"))
        surplus = len(archives) + incoming - keep
        return archives[:surplus] if surplus > 0 else []
