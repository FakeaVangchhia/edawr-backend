"""`uv run manage.py seed` — create sample data.

Replaces the old top-level `seed.py`. A **management command** is Django's
answer to "a script that needs the app configured": drop a module in
`<app>/management/commands/`, and its filename becomes the command name.
`manage.py` has already loaded settings and the model registry by the time
`handle()` runs, so there is no bootstrap code to write.

**What changed, and it is the biggest single win of the move:**

The old script called `Base.metadata.drop_all()` + `create_all()`, because
SQLAlchemy's `create_all` only ever *creates missing tables* — it never alters
an existing one. Dropping the whole database was the only way to pick up a model
change, which meant "add a column" and "erase all your data" were the same
operation.

Django has migrations built in. The schema is now owned by `manage.py migrate`,
so this command only touches **rows**:

    uv run manage.py makemigrations   # after editing models.py
    uv run manage.py migrate          # apply — keeps existing data
    uv run manage.py seed             # reset the sample rows

It is still destructive to *data*: it deletes every row in every table,
including admin accounts you added by hand. It does not touch uploaded files.
"""

import os

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import AdminUser, Category, Order, OrderItem, Product, User
from api.security import hash_password

# Lowercased to match the lookup in views/auth.py, which normalises the
# submitted email. Storing "Admin@edawr.local" verbatim would create an account
# that can never be logged into.
DEFAULT_ADMIN_EMAIL = os.getenv("SEED_ADMIN_EMAIL", "admin@edawr.local").strip().lower()
DEFAULT_ADMIN_PASSWORD = os.getenv("SEED_ADMIN_PASSWORD", "admin1234")
# Sample riders need a PIN or the mobile app cannot sign in at all. This is a
# development convenience with an obvious value; override it for anything real.
DEFAULT_RIDER_PIN = os.getenv("SEED_RIDER_PIN", "1234")


class Command(BaseCommand):
    help = "Delete all rows and load the eDawr sample dataset."

    def add_arguments(self, parser):
        """Command-line flags, via argparse.

            uv run manage.py seed --email you@example.com --password hunter2
        """
        parser.add_argument("--email", default=DEFAULT_ADMIN_EMAIL)
        parser.add_argument("--password", default=DEFAULT_ADMIN_PASSWORD)
        parser.add_argument("--rider-pin", default=DEFAULT_RIDER_PIN)

    # One transaction: either the whole dataset lands or none of it does.
    @transaction.atomic
    def handle(self, *args, **options):
        admin_email = options["email"].strip().lower()
        admin_password = options["password"]
        rider_pin = options["rider_pin"]
        rider_pin_hash = hash_password(rider_pin)

        # Children before parents — OrderItem.product is on_delete=PROTECT, so
        # deleting products first would raise ProtectedError.
        OrderItem.objects.all().delete()
        Order.objects.all().delete()
        Product.objects.all().delete()
        Category.objects.all().delete()
        User.objects.all().delete()
        AdminUser.objects.all().delete()

        # --- Admin login ------------------------------------------------
        AdminUser.objects.create(
            email=admin_email,
            password_hash=hash_password(admin_password),
        )

        # --- Staff --------------------------------------------------------
        manager = User.objects.create(
            name="Lalthanpuia", role=User.MANAGER, phone="+919000000001"
        )
        rider_a = User.objects.create(
            name="Zoramthanga",
            role=User.DELIVERY,
            phone="+919000000002",
            pin_hash=rider_pin_hash,
            base_latitude=23.7272,
            base_longitude=92.7178,
            service_radius_km=10.0,
        )
        User.objects.create(
            name="Vanlalruata",
            role=User.DELIVERY,
            phone="+919000000003",
            pin_hash=rider_pin_hash,
            base_latitude=23.7350,
            base_longitude=92.7250,
            service_radius_km=6.0,
        )

        # --- Categories ---------------------------------------------------
        categories = [
            Category(name="Vegetables", description="Fresh local produce"),
            Category(name="Dairy", description="Milk, curd, cheese"),
            Category(name="Staples", description="Rice, flour, pulses"),
            Category(name="Snacks", description="Packaged snacks"),
        ]
        # `bulk_create` is one INSERT for the whole list instead of one each.
        Category.objects.bulk_create(categories)

        # --- Products -------------------------------------------------------
        products = [
            Product(name="Milk", category="Dairy", brand="Amul", unit="litre",
                    price=62.0, cost_price=52.0, mrp=65.0, stock=40, reorder_level=10,
                    sku="DRY-MLK-001", description="Full cream milk, 1L pouch."),
            Product(name="Curd", category="Dairy", brand="Amul", unit="cup",
                    price=35.0, cost_price=28.0, mrp=38.0, stock=25, reorder_level=8,
                    sku="DRY-CRD-002", description="Set curd, 400g."),
            Product(name="Bread", category="Staples", brand="Britannia", unit="loaf",
                    price=45.0, cost_price=36.0, mrp=48.0, stock=18, reorder_level=6,
                    sku="STP-BRD-003", description="Whole wheat sandwich loaf."),
            Product(name="Rice", category="Staples", brand="Local", unit="kg",
                    price=58.0, cost_price=48.0, mrp=62.0, stock=120, reorder_level=25,
                    sku="STP-RCE-004", description="Aizawl local white rice."),
            Product(name="Potato", category="Vegetables", brand="Local", unit="kg",
                    price=32.0, cost_price=24.0, mrp=35.0, stock=80, reorder_level=20,
                    sku="VEG-POT-005", description="Fresh potatoes."),
            Product(name="Tomato", category="Vegetables", brand="Local", unit="kg",
                    price=44.0, cost_price=34.0, mrp=48.0, stock=35, reorder_level=12,
                    sku="VEG-TOM-006", description="Vine-ripened tomatoes."),
            Product(name="Onion", category="Vegetables", brand="Local", unit="kg",
                    price=38.0, cost_price=30.0, mrp=42.0, stock=0, reorder_level=15,
                    sku="VEG-ONI-007", description="Currently out of stock."),
            Product(name="Biscuits", category="Snacks", brand="Parle", unit="pack",
                    price=20.0, cost_price=15.0, mrp=22.0, stock=60, reorder_level=20,
                    sku="SNK-BIS-008", description="Glucose biscuits, 150g."),
            Product(name="Instant Noodles", category="Snacks", brand="Maggi", unit="pack",
                    price=14.0, cost_price=11.0, mrp=15.0, stock=90, reorder_level=30,
                    sku="SNK-NDL-009", description="Masala flavour, 70g."),
            Product(name="Cooking Oil", category="Staples", brand="Fortune", unit="litre",
                    price=145.0, cost_price=125.0, mrp=155.0, stock=22, reorder_level=8,
                    sku="STP-OIL-010", status="inactive",
                    description="Sunflower oil — hidden from the storefront (status=inactive)."),
        ]
        Product.objects.bulk_create(products)
        by_name = {p.name: p for p in Product.objects.all()}

        # --- Orders -----------------------------------------------------------
        # NOTE: nothing in the app creates orders any more — that lived in the
        # deleted WhatsApp webhook. These exist so the dashboards have data.
        # See the README's "Known gaps" section.
        pending = Order.objects.create(
            customer_phone="+919812345678",
            customer_name="Lalrinsangi",
            customer_address="Chanmari, Aizawl",
            customer_latitude=23.7300,
            customer_longitude=92.7200,
            status=Order.PENDING,
        )
        assigned = Order.objects.create(
            customer_phone="+919887654321",
            customer_name="Remruatpuia",
            customer_address="Zarkawt, Aizawl",
            customer_latitude=23.7260,
            customer_longitude=92.7190,
            status=Order.ASSIGNED,
            delivery_boy=rider_a,
        )
        delivered = Order.objects.create(
            customer_phone="+919776655443",
            customer_name="Lalduhawma",
            customer_address="Bazar Bawn, Aizawl",
            customer_latitude=23.7280,
            customer_longitude=92.7165,
            status=Order.DELIVERED,
            delivery_boy=rider_a,
        )

        OrderItem.objects.bulk_create([
            OrderItem(order=pending, product=by_name["Milk"], quantity=2, name="Milk", price=62.0),
            OrderItem(order=pending, product=by_name["Bread"], quantity=1, name="Bread", price=45.0),
            OrderItem(order=assigned, product=by_name["Rice"], quantity=5, name="Rice", price=58.0),
            OrderItem(order=assigned, product=by_name["Potato"], quantity=2, name="Potato", price=32.0),
            OrderItem(order=delivered, product=by_name["Biscuits"], quantity=3, name="Biscuits", price=20.0),
        ])

        # `self.stdout.write` rather than print(): it respects --verbosity and
        # is capturable in tests. `self.style` adds colour.
        self.stdout.write("Seeded:")
        self.stdout.write(f"  admin login      {admin_email} / {admin_password}")
        self.stdout.write(f"  rider login      +919000000002 / {rider_pin}  (PIN shared by both riders)")
        self.stdout.write(f"  staff            {manager.name} + 2 riders")
        self.stdout.write(f"  products         {len(products)} ({len(products) - 1} active, 1 inactive)")
        self.stdout.write(f"  categories       {len(categories)}")
        self.stdout.write("  orders           3 (1 Pending, 1 Assigned, 1 Delivered)")
        self.stdout.write(self.style.SUCCESS("Done."))
