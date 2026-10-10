"""Lower-case every catalogue status so exact matches find every row.

The storefront used to filter with `status__iexact="active"`, which tolerated
"Active" and "ACTIVE" from the Supabase-era imports — and defeated the
`product_store_idx` index, because Postgres cannot serve `UPPER(status) = …`
from a plain b-tree. The queries are exact now, so any row still spelled with a
capital would silently vanish from the shop. This rewrites them once; the
serializers have validated the two lower-case values for every write since.
"""

from django.db import migrations
from django.db.models.functions import Lower


def lowercase_statuses(apps, schema_editor):
    for name in ("Product", "Category", "Promo"):
        model = apps.get_model("api", name)
        model.objects.exclude(status__in=("active", "inactive")).update(
            status=Lower("status")
        )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0014_promo_suggestion"),
    ]

    operations = [
        # Nothing to reverse: the values were already meant to be lower-case.
        migrations.RunPython(lowercase_statuses, migrations.RunPython.noop),
    ]
