"""Store the fulfilment outcome that was previously only ever derived.

The two new columns are stamped by `Order.advance_status` from now on, but every
order already delivered predates that and would read as NULL — which the
analytics endpoints would correctly interpret as "never delivered", quietly
reporting an empty history to a store that has one.

So the columns are backfilled from the timestamps that were always there.
`delivered_at - created_at` is exactly the arithmetic the old
`fulfilment_minutes` property did, and `promised_minutes` has been on the row
since 0004, so the backfilled values agree with what the property would have
returned. This is the one and only moment they can be recomputed; from here they
are a record, not a derivation.

Batched with `bulk_update` rather than a per-row save: this runs against
production order history, and 0003 already set the precedent that a migration
touching every row does so deliberately.
"""

from django.db import migrations, models


def backfill_fulfilment(apps, schema_editor):
    Order = apps.get_model("api", "Order")
    delivered = Order.objects.filter(delivered_at__isnull=False).only(
        "id", "created_at", "delivered_at", "promised_minutes"
    )
    batch = []
    for order in delivered.iterator(chunk_size=500):
        elapsed = int((order.delivered_at - order.created_at).total_seconds() // 60)
        order.delivered_in_minutes = elapsed
        order.was_late = elapsed > order.promised_minutes
        batch.append(order)
        if len(batch) >= 500:
            Order.objects.bulk_update(batch, ["delivered_in_minutes", "was_late"])
            batch = []
    if batch:
        Order.objects.bulk_update(batch, ["delivered_in_minutes", "was_late"])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0005_roles_and_audit'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='delivered_in_minutes',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='was_late',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_fulfilment, migrations.RunPython.noop, elidable=False
        ),
    ]
