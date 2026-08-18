"""Create or update one console account, without touching anything else.

**This exists because `seed` cannot be run on a live database.** `seed` opens by
deleting every row in every table — that is correct for a development fixture
and catastrophic in production, and it is also the *only* path that has ever
created an admin. So the documented launch procedure was "run `seed` once on the
empty production database and never again", which leaves a store with no way to
add a second administrator, recover a forgotten password, or replace the
published default credentials after go-live.

This command is the missing half: idempotent, additive, and safe to run against
a database with orders in it.

    uv run manage.py seed_admin --email owner@example.com --password '...' --role admin
    uv run manage.py seed_admin --email owner@example.com --password 'new one'  # reset

Passing an existing email updates that account rather than failing, which is
what makes it a password-reset tool as well as a create tool.
"""

from django.core.management.base import BaseCommand, CommandError

from api.models import AdminUser
from api.security import hash_password

MIN_PASSWORD_LENGTH = 8


class Command(BaseCommand):
    help = "Create or update a single admin-console account. Deletes nothing."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--password",
            required=False,
            help="Omit when only changing the role or name of an existing account.",
        )
        parser.add_argument(
            "--role",
            default=AdminUser.ADMIN,
            choices=[choice for choice, _ in AdminUser.ROLE_CHOICES],
            help="Defaults to admin — this command's main use is minting the first one.",
        )
        parser.add_argument("--name", default="")
        parser.add_argument(
            "--deactivate",
            action="store_true",
            help="Revoke this account's access instead of granting it.",
        )

    def handle(self, *args, **options):
        # Lowercased because `LoginView` lowercases before looking up. An account
        # stored as "Owner@x.com" would otherwise be created successfully and
        # then be unable to sign in, which is a confusing way to fail.
        email = options["email"].strip().lower()
        password = options["password"]
        role = options["role"]

        if not email or "@" not in email:
            raise CommandError("--email must be an email address.")

        account = AdminUser.objects.filter(email=email).first()
        creating = account is None

        if creating and not password:
            raise CommandError(
                "--password is required when creating a new account."
            )
        if password and len(password) < MIN_PASSWORD_LENGTH:
            raise CommandError(
                f"--password must be at least {MIN_PASSWORD_LENGTH} characters."
            )

        if creating:
            account = AdminUser(email=email)

        if password:
            account.password_hash = hash_password(password)
        if options["name"]:
            account.name = options["name"]

        # Refuse to remove the last way in. Demoting or deactivating the only
        # Admin from the shell has exactly the consequence the API's own guard
        # exists to prevent, and "I did it from the command line" is not a reason
        # the resulting lockout is any easier to fix.
        losing_admin = (
            not creating
            and account.role == AdminUser.ADMIN
            and (role != AdminUser.ADMIN or options["deactivate"])
        )
        if losing_admin and not AdminUser.objects.filter(
            role=AdminUser.ADMIN, is_active=True
        ).exclude(pk=account.pk).exists():
            raise CommandError(
                f"{email} is the last active Admin. Create another Admin first, "
                "or this database could no longer be administered."
            )

        account.role = role
        account.is_active = not options["deactivate"]
        account.save()

        verb = "Created" if creating else "Updated"
        state = "active" if account.is_active else "DEACTIVATED"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {account.get_role_display()} account {email} ({state})."
            )
        )
        if password:
            self.stdout.write("Password set. It is not echoed and cannot be read back.")
