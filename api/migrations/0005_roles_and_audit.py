"""Two console roles, and a record of who used them.

The interesting operation here is `promote_existing_admins`, not the AddFields.

`AdminUser.role` defaults to MANAGER, which is the right default for a *new*
row — the safe failure is the lesser privilege. It is the wrong value for the
rows that already exist. Every account created before this migration had no role
concept at all and could do everything, so applying the field default to them
would silently demote the store owner and leave nobody able to reach
`/api/admins` — the one screen that could put it back. The database would be
locked out of its own administration by an upgrade.

So the AddField runs with the model default, and then a data migration
immediately rewrites every pre-existing row to ADMIN. `0003_quick_commerce` is
the worked example this follows: migrations are source code, and they have to
survive the data that is already there.

The reverse is a deliberate no-op. Rolling back cannot know which accounts were
ADMIN before, and guessing would be worse than leaving the column to be dropped.
"""


import django.utils.timezone
from django.db import migrations, models


def promote_existing_admins(apps, schema_editor):
    """Every account that predates the role column was already all-powerful.

    Matched on `role="manager"` rather than on everything, so this is idempotent
    and re-running it cannot clobber a manager created after the fact — though in
    practice it runs once, immediately after the AddField that wrote that value.
    """
    AdminUser = apps.get_model("api", "AdminUser")
    AdminUser.objects.filter(role="manager").update(role="admin")


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0004_delivery_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='adminuser',
            name='last_login_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='adminuser',
            name='name',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='adminuser',
            name='role',
            field=models.CharField(choices=[('admin', 'Admin'), ('manager', 'Manager')], default='manager', max_length=16),
        ),
        migrations.AlterField(
            model_name='category',
            name='status',
            field=models.CharField(choices=[('active', 'Active'), ('inactive', 'Inactive')], default='active', max_length=32),
        ),
        migrations.AlterField(
            model_name='product',
            name='status',
            field=models.CharField(choices=[('active', 'Active'), ('inactive', 'Inactive')], db_index=True, default='active', max_length=32),
        ),
        migrations.RunPython(
            promote_existing_admins, migrations.RunPython.noop, elidable=False
        ),
        migrations.CreateModel(
            name='AuditLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_kind', models.CharField(choices=[('admin', 'Admin'), ('rider', 'Rider'), ('system', 'System')], default='system', max_length=16)),
                ('actor_id', models.IntegerField(blank=True, null=True)),
                ('actor_label', models.CharField(blank=True, default='', max_length=255)),
                ('actor_role', models.CharField(blank=True, default='', max_length=16)),
                ('action', models.CharField(choices=[('create', 'Create'), ('update', 'Update'), ('delete', 'Delete'), ('login', 'Login'), ('status', 'Status'), ('assign', 'Assign'), ('cancel', 'Cancel')], max_length=16)),
                ('entity', models.CharField(max_length=32)),
                ('entity_id', models.IntegerField(blank=True, null=True)),
                ('summary', models.CharField(blank=True, default='', max_length=255)),
                ('changes', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ],
            options={
                'db_table': 'audit_log',
                'ordering': ['-created_at', '-id'],
                'indexes': [models.Index(fields=['entity', 'entity_id'], name='audit_entity_idx'), models.Index(fields=['actor_kind', 'actor_id'], name='audit_actor_idx')],
            },
        ),
    ]
