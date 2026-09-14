"""Drop the never-recorded `login` audit action from the choices.

No SQL: choices live in Python, and no row ever carried the value.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0015_normalise_catalogue_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(choices=[('create', 'Create'), ('update', 'Update'), ('delete', 'Delete'), ('status', 'Status'), ('assign', 'Assign'), ('cancel', 'Cancel')], max_length=16),
        ),
    ]
