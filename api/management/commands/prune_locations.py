"""Delete position history that has outlived its purpose.

    uv run manage.py prune_locations --dry-run   # say what would go
    uv run manage.py prune_locations             # delete it

**This must be scheduled, and nothing else does its job.** The breadcrumb trail
in `order_location_pings` is append-only and grows with every delivery; left
alone it becomes a permanent, indefinitely detailed record of where this store's
customers live. A retention promise that depends on somebody remembering to run
a command is not a retention promise, so put this on a timer — see
`deployment.md`.

Two different things go, for two different reasons.

**Breadcrumbs past the retention window.** `LOCATION_PING_RETENTION_DAYS`
(default 30) is set by what the trail is *for*: settling "nobody came" after a
failed delivery, a conversation that happens within days. Keeping it longer
would be keeping it for no stated purpose.

**Customer positions on orders that have ended.** These should not exist:
`Order.advance_status` deletes one the moment an order reaches a terminal state,
in the model rather than in a view precisely so no route can skip it. A row here
therefore belongs to an order that got to `Delivered` or `Cancelled` some other
way — a bulk `update()`, a data migration, a fixture — and this is the backstop
that catches that rather than the mechanism relied on. Seeing a non-zero count
here regularly means something is bypassing the state machine, which is worth
investigating on its own.

`RiderLocation` is not pruned and does not grow: it holds one row per rider,
overwritten in place. A rider's row goes when the rider does, by CASCADE.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from datetime import timedelta

from api import location
from api.models import Order, OrderCustomerLocation, OrderLocationPing


class Command(BaseCommand):
    help = "Delete expired location breadcrumbs and orphaned customer positions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would be deleted."
        )

    def handle(self, *args, **options):
        days = settings.LOCATION_PING_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=days)

        # Counted before anything is deleted so the dry run and the real run
        # report the same numbers for the same database.
        expired = OrderLocationPing.objects.filter(received_at__lt=cutoff).count()
        orphans = OrderCustomerLocation.objects.filter(
            order__status__in=Order.TERMINAL
        ).count()

        self.stdout.write(f"Retention window:              {days} days")
        self.stdout.write(f"Breadcrumbs older than cutoff: {expired}")
        self.stdout.write(f"Customer positions on ended orders: {orphans}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run - nothing was deleted."))
            return

        if not expired and not orphans:
            self.stdout.write(self.style.SUCCESS("Nothing to prune."))
            return

        # No confirmation prompt, unlike `demo_clear`. This is meant to run
        # unattended on a schedule, and it only ever deletes rows that are
        # already past the point of being useful — there is no judgement call
        # for an operator to make, and a prompt would mean it silently never
        # ran under cron.
        deleted_pings, deleted_positions = location.prune()

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_pings} breadcrumb(s) "
                f"and {deleted_positions} customer position(s)."
            )
        )
