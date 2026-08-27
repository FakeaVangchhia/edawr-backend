"""Idempotent checkout, cash reconciliation, and revocable sessions.

Three unrelated features in one migration because they are one deploy, and a
half-migrated database is worse than a large one.

**Why none of these needs the three-step dance `0003` uses.** Every column added
here is either nullable with no default, or non-nullable with a constant one —
so `AddField` alone is safe on a table that already has rows:

- `idempotency_key` is nullable *and* unique, which reads like the trap
  `0003_quick_commerce` had to work around for `tracking_token`. It is not the
  same case. That one had a **callable default**, so every existing row would
  have been handed the same generated value and the unique index would have
  refused to build. This column has no default at all: existing orders get NULL,
  and Postgres treats NULLs as distinct in a unique index, so any number of
  key-less rows coexist. (SQLite agrees. The standard requires it.)
- `token_version` is `default=0`, a constant, which is exactly what a default is
  for.

**The one thing that does need data written: historical deliveries.** Before
this migration, marking an order Delivered was the *only* record that cash had
changed hands — `payment_method = "cod"` said money was owed and nothing said it
arrived. Adding `amount_collected` without backfilling would leave every past
delivery reading as collected-nothing, so the first cash report a manager opens
would show the entire history of the shop as a shortfall and be worse than no
report at all.

So every already-Delivered order is backfilled with what the system implicitly
believed at the time: paid in full, at the moment of delivery. That is an
assumption, and it is the only one available — but it is the assumption the old
code was already making silently, now written down where it can be seen.

Orders delivered from here on are stamped by `Order.advance_status()` instead.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_historical_collections(apps, schema_editor):
    """Record what the old code assumed: a delivered order was paid in full.

    Uses the historical model, not the imported `Order` — the imported class is
    today's shape, and a migration has to keep working when today moves on.

    Batched rather than one `.save()` per row. Aizawl's order history is small
    today and will not always be, and a migration that is fine at 500 rows and
    locks the table at 500,000 is a migration you find out about during a deploy.
    """
    Order = apps.get_model("api", "Order")

    delivered = Order.objects.filter(
        status="Delivered", delivered_at__isnull=False, paid_at__isnull=True
    ).only("id", "delivered_at", "grand_total", "paid_at", "amount_collected")

    batch = []
    for order in delivered.iterator(chunk_size=500):
        order.paid_at = order.delivered_at
        order.amount_collected = order.grand_total
        batch.append(order)
        if len(batch) >= 500:
            Order.objects.bulk_update(batch, ["paid_at", "amount_collected"])
            batch.clear()

    if batch:
        Order.objects.bulk_update(batch, ["paid_at", "amount_collected"])


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0008_settings_time_defaults"),
    ]

    operations = [
        # --- revocable sessions ------------------------------------------
        migrations.AddField(
            model_name="adminuser",
            name="token_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="user",
            name="token_version",
            field=models.PositiveIntegerField(default=0),
        ),
        # --- idempotent checkout ------------------------------------------
        migrations.AddField(
            model_name="order",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        # --- cash reconciliation ------------------------------------------
        migrations.AddField(
            model_name="order",
            name="paid_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="amount_collected",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="collected_by",
            field=models.ForeignKey(
                blank=True,
                db_column="collected_by_id",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="collected_orders",
                to="api.user",
            ),
        ),
        migrations.RunPython(
            backfill_historical_collections,
            # Reversing drops the two columns this wrote, so there is nothing
            # for a reverse function to undo.
            migrations.RunPython.noop,
        ),
    ]
