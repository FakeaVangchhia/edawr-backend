"""Add the delivery tier an order was placed on.

A plain AddField is enough here, and the flat `default='instant'` is truthful
rather than merely convenient: every row that already exists was placed when
there was one delivery speed, and that speed was the fifteen-minute one. Their
`promised_minutes` already says 15, so backfilling them as Instant leaves the
two columns agreeing with each other and with what those customers were told.

No RunPython, therefore — the only case that would need one is a default that
lies about history.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0003_quick_commerce'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='delivery_type',
            field=models.CharField(choices=[('instant', 'Instant'), ('slow', 'Slow')], default='instant', max_length=16),
        ),
    ]
