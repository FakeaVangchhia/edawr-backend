"""Store settings, a failed-delivery outcome, and honest coordinates.

The hand-written part is `blank_out_fabricated_coordinates`. Making the columns
nullable is not enough on its own: every row already in the table carries the
store's own coordinates, because that is what the old `default=` put there when
a checkout arrived without a position. Leaving them is worse than the NULL this
migration makes possible -- they are not missing data, they are wrong data that
reads as right, and the dispatch code is about to start trusting them.
"""

import django.core.validators
import django.utils.timezone
from django.db import migrations, models

# The values the removed `default=` wrote. Matched exactly, on purpose: these
# are the literal floats Django stored, not a measurement, so equality is the
# correct test and a tolerance would sweep up real addresses near the shop.
FABRICATED_LATITUDE = 23.7272
FABRICATED_LONGITUDE = 92.7178


def blank_out_fabricated_coordinates(apps, schema_editor):
    """Turn "at the counter" back into "we do not know".

    A customer standing on the store's exact coordinates to the fourth decimal
    place is not a thing that happens; a checkout that sent no position is what
    happened. Every such row made its rider look 0.00 km away, which is what
    made the service-radius filter match everyone.
    """
    Order = apps.get_model("api", "Order")
    Order.objects.filter(
        customer_latitude=FABRICATED_LATITUDE,
        customer_longitude=FABRICATED_LONGITUDE,
    ).update(customer_latitude=None, customer_longitude=None)


def restore_fabricated_coordinates(apps, schema_editor):
    """Reverse: the columns become NOT NULL again, so NULL needs *a* value."""
    Order = apps.get_model("api", "Order")
    Order.objects.filter(customer_latitude__isnull=True).update(
        customer_latitude=FABRICATED_LATITUDE,
        customer_longitude=FABRICATED_LONGITUDE,
    )


def create_settings_row(apps, schema_editor):
    """The singleton, so nothing has to cope with "not configured yet".

    `StoreSettings.load()` would create it on first read anyway. Doing it here
    means the row exists before the first request rather than being written by
    whichever request happens to arrive first -- which on a multi-instance
    deploy is several requests racing to create the same pk.
    """
    StoreSettings = apps.get_model("api", "StoreSettings")
    StoreSettings.objects.get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0006_fulfilment_outcome'),
    ]

    operations = [
        migrations.CreateModel(
            name='StoreSettings',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('is_accepting_orders', models.BooleanField(default=True)),
                ('closed_message', models.CharField(blank=True, default='', max_length=255)),
                ('opens_at', models.TimeField(default='07:00')),
                ('closes_at', models.TimeField(default='22:00')),
                ('delivery_radius_km', models.FloatField(default=8.0, validators=[django.core.validators.MinValueValidator(0.1)])),
                ('store_latitude', models.FloatField(default=23.7272)),
                ('store_longitude', models.FloatField(default=92.7178)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'verbose_name_plural': 'store settings',
                'db_table': 'store_settings',
            },
        ),
        migrations.RemoveField(
            model_name='order',
            name='offered_to_delivery_boy',
        ),
        migrations.AddField(
            model_name='order',
            name='restocked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='order',
            name='customer_latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='order',
            name='customer_longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.RunPython(
            blank_out_fabricated_coordinates, restore_fabricated_coordinates
        ),
        migrations.RunPython(create_settings_row, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(choices=[('Placed', 'Placed'), ('Packing', 'Packing'), ('Ready', 'Ready for pickup'), ('Dispatched', 'Out for delivery'), ('Delivered', 'Delivered'), ('Cancelled', 'Cancelled'), ('Failed', 'Delivery failed')], db_index=True, default='Placed', max_length=32),
        ),
    ]
