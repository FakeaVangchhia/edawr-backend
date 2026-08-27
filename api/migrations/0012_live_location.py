"""Live position tracking: three new tables, nothing altered.

**Safe on existing data, and unusually so for this project.** Every operation
here is a CreateModel — no column is added to a populated table, no vocabulary
is renamed, no constraint is applied to rows that predate it. Compare
`0003_quick_commerce`, which had to backfill totals, dedupe category names
before a unique constraint, and populate tracking tokens row by row. There is
nothing of that kind to do here: all three tables start empty and stay empty
until a handset reports a position.

The one thing worth knowing is what is *not* here. `Order.customer_latitude`
and `customer_longitude` are untouched. The customer's live position is a
separate row in `order_customer_locations`, precisely so a fix taken during
delivery can never overwrite the one checkout was decided on — see the comment
above `OrderCustomerLocation` in `api/models.py`.

Rolling back drops the three tables and loses every recorded position. Nothing
else depends on them — dispatch still ranks on `User.base_latitude` — so the
reverse costs telemetry and no orders.
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0011_customer_accounts'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderCustomerLocation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('accuracy_m', models.FloatField(blank=True, null=True)),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('order', models.OneToOneField(db_column='order_id', on_delete=django.db.models.deletion.CASCADE, related_name='customer_location', to='api.order')),
            ],
            options={
                'db_table': 'order_customer_locations',
            },
        ),
        migrations.CreateModel(
            name='RiderLocation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('accuracy_m', models.FloatField(blank=True, null=True)),
                ('speed_kmh', models.FloatField(blank=True, null=True)),
                ('heading', models.FloatField(blank=True, null=True)),
                ('recorded_at', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('order', models.ForeignKey(blank=True, db_column='order_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='api.order')),
                ('rider', models.OneToOneField(db_column='rider_id', on_delete=django.db.models.deletion.CASCADE, related_name='location', to='api.user')),
            ],
            options={
                'db_table': 'rider_locations',
            },
        ),
        migrations.CreateModel(
            name='OrderLocationPing',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('accuracy_m', models.FloatField(blank=True, null=True)),
                ('recorded_at', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('order', models.ForeignKey(db_column='order_id', on_delete=django.db.models.deletion.CASCADE, related_name='location_pings', to='api.order')),
                ('rider', models.ForeignKey(blank=True, db_column='rider_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='location_pings', to='api.user')),
            ],
            options={
                'db_table': 'order_location_pings',
                'indexes': [models.Index(fields=['order', 'received_at'], name='ping_order_idx'), models.Index(fields=['received_at'], name='ping_pruning_idx')],
            },
        ),
    ]
