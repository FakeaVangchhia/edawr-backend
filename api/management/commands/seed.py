"""`uv run manage.py seed` — create sample data.

A **management command** is Django's answer to "a script that needs the app
configured": drop a module in `<app>/management/commands/`, and its filename
becomes the command name. `manage.py` has already loaded settings and the model
registry by the time `handle()` runs, so there is no bootstrap code to write.

The schema is owned by `manage.py migrate`; this command only touches **rows**:

    uv run manage.py makemigrations   # after editing models.py
    uv run manage.py migrate          # apply — keeps existing data
    uv run manage.py seed             # reset the sample rows

It is destructive to *data*: it deletes every row in every table, including
admin accounts you added by hand. It does not touch uploaded files.

The sample orders are built through `api.pricing` rather than with hand-written
totals, so seeded data obeys the same fee rules as a real checkout. Totals typed
in by hand are how a demo ends up showing arithmetic the live site would never
produce.
"""

import os
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import (
    STORE_LATITUDE,
    STORE_LONGITUDE,
    AdminUser,
    Category,
    Customer,
    Order,
    OrderItem,
    OrderRejection,
    Product,
    StoreSettings,
    User,
)
from api.pricing import compute_charges, money, resolve_tier
from api.security import hash_password

# Lowercased to match the lookup in views/auth.py, which normalises the
# submitted email. Storing "Admin@edawr.local" verbatim would create an account
# that can never be logged into.
DEFAULT_ADMIN_EMAIL = os.getenv("SEED_ADMIN_EMAIL", "admin@edawr.local").strip().lower()
DEFAULT_ADMIN_PASSWORD = os.getenv("SEED_ADMIN_PASSWORD", "admin1234")
# Not "1234": UserSerializer.validate_pin rejects the obvious sequences, and a
# seed that plants a credential the API itself would refuse is a seed that
# teaches the wrong habit.
DEFAULT_RIDER_PIN = os.getenv("SEED_RIDER_PIN", "4813")


# name, description, sort order
CATEGORIES = [
    ("Fruits & Vegetables", "Fresh local produce, picked daily", 1),
    ("Dairy & Bread", "Milk, curd, cheese, bakery", 2),
    ("Snacks & Munchies", "Chips, biscuits, namkeen", 3),
    ("Cold Drinks & Juices", "Chilled and ready to drink", 4),
    ("Instant & Frozen", "Noodles, ready meals, frozen", 5),
    ("Staples", "Rice, flour, pulses, oil", 6),
    ("Personal Care", "Everyday essentials", 7),
    ("Household", "Cleaning and home supplies", 8),
]

# name, category, brand, unit, price, mrp, stock
# MRP above price wherever there is a genuine saving — the storefront renders a
# struck-out MRP and a discount badge from exactly that difference, and shows
# neither when the two are equal.
PRODUCTS = [
    ("Tomato", "Fruits & Vegetables", "Local", "500 g", "24.00", "32.00", 60),
    ("Onion", "Fruits & Vegetables", "Local", "1 kg", "38.00", "45.00", 80),
    ("Potato", "Fruits & Vegetables", "Local", "1 kg", "32.00", "38.00", 75),
    ("Banana (Robusta)", "Fruits & Vegetables", "Local", "6 pcs", "42.00", "50.00", 40),
    ("Green Chilli", "Fruits & Vegetables", "Local", "100 g", "12.00", "15.00", 35),
    ("Coriander Leaves", "Fruits & Vegetables", "Local", "100 g", "10.00", "12.00", 0),
    ("Amul Taaza Milk", "Dairy & Bread", "Amul", "1 L", "62.00", "66.00", 48),
    ("Amul Masti Curd", "Dairy & Bread", "Amul", "400 g", "35.00", "40.00", 30),
    ("Britannia Brown Bread", "Dairy & Bread", "Britannia", "400 g", "45.00", "50.00", 22),
    ("Amul Butter", "Dairy & Bread", "Amul", "100 g", "58.00", "62.00", 26),
    ("Farm Eggs", "Dairy & Bread", "Local", "6 pcs", "48.00", "54.00", 40),
    ("Lay's Classic Salted", "Snacks & Munchies", "Lay's", "52 g", "20.00", "20.00", 90),
    ("Parle-G Biscuits", "Snacks & Munchies", "Parle", "250 g", "25.00", "30.00", 120),
    ("Haldiram Aloo Bhujia", "Snacks & Munchies", "Haldiram", "200 g", "52.00", "60.00", 45),
    ("Dark Fantasy Choco Fills", "Snacks & Munchies", "Sunfeast", "75 g", "35.00", "40.00", 38),
    ("Coca-Cola", "Cold Drinks & Juices", "Coca-Cola", "750 ml", "40.00", "45.00", 60),
    ("Real Mixed Fruit Juice", "Cold Drinks & Juices", "Real", "1 L", "110.00", "125.00", 25),
    ("Bisleri Water", "Cold Drinks & Juices", "Bisleri", "1 L", "20.00", "22.00", 100),
    ("Maggi Masala Noodles", "Instant & Frozen", "Maggi", "4 x 70 g", "56.00", "64.00", 85),
    ("McCain French Fries", "Instant & Frozen", "McCain", "420 g", "125.00", "140.00", 18),
    ("Knorr Sweet Corn Soup", "Instant & Frozen", "Knorr", "44 g", "55.00", "60.00", 24),
    ("Aizawl Local Rice", "Staples", "Local", "5 kg", "285.00", "320.00", 40),
    ("Aashirvaad Atta", "Staples", "Aashirvaad", "5 kg", "255.00", "285.00", 30),
    ("Fortune Sunflower Oil", "Staples", "Fortune", "1 L", "145.00", "165.00", 28),
    ("Tata Salt", "Staples", "Tata", "1 kg", "28.00", "30.00", 55),
    ("Toor Dal", "Staples", "Local", "1 kg", "165.00", "180.00", 32),
    ("Colgate Strong Teeth", "Personal Care", "Colgate", "200 g", "112.00", "125.00", 26),
    ("Dove Soap", "Personal Care", "Dove", "3 x 100 g", "165.00", "190.00", 20),
    ("Head & Shoulders Shampoo", "Personal Care", "H&S", "340 ml", "310.00", "355.00", 14),
    ("Surf Excel Easy Wash", "Household", "Surf Excel", "1 kg", "125.00", "140.00", 22),
    ("Vim Dishwash Gel", "Household", "Vim", "500 ml", "115.00", "130.00", 18),
    ("Harpic Toilet Cleaner", "Household", "Harpic", "500 ml", "92.00", "105.00", 16),
    ("Premium Basmati Rice", "Staples", "India Gate", "1 kg", "165.00", "185.00", 12),
]


class Command(BaseCommand):
    help = "Delete all rows and load the eDawr sample dataset."

    def add_arguments(self, parser):
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
        OrderRejection.objects.all().delete()
        OrderItem.objects.all().delete()
        Order.objects.all().delete()
        Product.objects.all().delete()
        Category.objects.all().delete()
        User.objects.all().delete()
        AdminUser.objects.all().delete()
        # After Order, though `Order.customer` is SET_NULL and would not have
        # stopped it. Deleting these first would quietly blank the FK on orders
        # that are about to be deleted anyway — harmless, but it reads as if the
        # order matters, and here it genuinely does not.
        Customer.objects.all().delete()

        # Deliberately *not* deleted. StoreSettings is a singleton whose absence
        # would mean the shop has no opening hours and no delivery radius, and
        # `manage.py seed` is a "give me sample data" command, not a factory
        # reset of the store's operating configuration. Re-opened instead, so a
        # developer who paused the store yesterday can seed and get to work.
        store = StoreSettings.load()
        store.is_accepting_orders = True
        store.closed_message = ""
        store.save(update_fields=["is_accepting_orders", "closed_message"])

        # `role=ADMIN`, explicitly. `AdminUser.role` defaults to `manager`, so
        # this account used to be seeded as one — which left a freshly seeded
        # development environment unable to reach `/accounts` or `/audit` at
        # all. Two of the console's ten screens were unreachable, and the
        # Admin-vs-Manager gating that `api/permissions.py` exists to enforce
        # could not be exercised without hand-editing a row.
        #
        # Development wants to see everything; production never runs this
        # command and creates its first account with
        # `seed_admin --role admin`, where the choice is deliberate.
        AdminUser.objects.create(
            email=admin_email,
            password_hash=hash_password(admin_password),
            role=AdminUser.ADMIN,
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
        rider_b = User.objects.create(
            name="Vanlalruata",
            role=User.DELIVERY,
            phone="+919000000003",
            pin_hash=rider_pin_hash,
            base_latitude=23.7350,
            base_longitude=92.7250,
            service_radius_km=6.0,
        )

        # --- Categories ---------------------------------------------------
        Category.objects.bulk_create([
            Category(name=name, description=description, sort_order=order)
            for name, description, order in CATEGORIES
        ])

        # --- Products -----------------------------------------------------
        Product.objects.bulk_create([
            Product(
                name=name,
                category=category,
                brand=brand,
                unit=unit,
                price=Decimal(price),
                mrp=Decimal(mrp),
                # A plausible margin, so the admin console's cost column is not
                # uniformly zero. Never shown on the storefront.
                cost_price=money(Decimal(price) * Decimal("0.78")),
                stock=stock,
                reorder_level=10,
                sku=f"EDW-{index:04d}",
                description=f"{brand} {name}, {unit}.",
            )
            for index, (name, category, brand, unit, price, mrp, stock) in enumerate(
                PRODUCTS, start=1
            )
        ])
        by_name = {product.name: product for product in Product.objects.all()}

        # --- Orders, one per interesting state -----------------------------
        now = timezone.now()

        placed = self._order(
            "Lalrinsangi", "+919812345678", "Chanmari, Aizawl",
            [(by_name["Amul Taaza Milk"], 2), (by_name["Britannia Brown Bread"], 1)],
            status=Order.PLACED, created_at=now,
            latitude=23.7300, longitude=92.7200,
        )
        # Mixed tiers on purpose: the kanban's whole job is to let a packer see
        # at a glance which orders are on the fifteen-minute clock, and a board
        # where every card says the same thing demonstrates nothing.
        packing = self._order(
            "Remruatpuia", "+919887654321", "Zarkawt, Aizawl",
            [(by_name["Maggi Masala Noodles"], 2), (by_name["Lay's Classic Salted"], 3)],
            status=Order.PACKING, created_at=now,
            latitude=23.7260, longitude=92.7190,
            delivery_type=Order.SLOW,
        )
        ready = self._order(
            "Zonunmawii", "+919765432100", "Dawrpui, Aizawl",
            [(by_name["Aizawl Local Rice"], 1), (by_name["Fortune Sunflower Oil"], 1)],
            status=Order.READY, created_at=now,
            latitude=23.7290, longitude=92.7185,
        )
        dispatched = self._order(
            "Lalduhawma", "+919776655443", "Bazar Bawn, Aizawl",
            [(by_name["Farm Eggs"], 2), (by_name["Amul Butter"], 1)],
            status=Order.DISPATCHED, created_at=now, rider=rider_a,
            latitude=23.7280, longitude=92.7165,
            delivery_type=Order.SLOW,
        )
        delivered = self._order(
            "Vanlalhruaii", "+919845612300", "Ramhlun, Aizawl",
            [(by_name["Parle-G Biscuits"], 4), (by_name["Bisleri Water"], 2)],
            status=Order.DELIVERED, created_at=now, rider=rider_a,
            latitude=23.7330, longitude=92.7220,
        )

        # One rejection, so the stalled view and the rider feed have something
        # real to demonstrate: rider_b will not see this order.
        OrderRejection.objects.create(order=ready, rider=rider_b)

        self.stdout.write("Seeded:")
        self.stdout.write(f"  admin login      {admin_email} / {admin_password}")
        self.stdout.write(f"  rider login      +919000000002 / {rider_pin}  (PIN shared by both riders)")
        self.stdout.write(f"  staff            {manager.name} + 2 riders")
        self.stdout.write(f"  categories       {len(CATEGORIES)}")
        self.stdout.write(f"  products         {len(PRODUCTS)}")
        self.stdout.write(
            "  orders           5 — "
            f"#{placed.pk} Placed, #{packing.pk} Packing, #{ready.pk} Ready, "
            f"#{dispatched.pk} Dispatched, #{delivered.pk} Delivered"
        )
        self.stdout.write(f"  track one at     /order/{placed.tracking_token}")
        self.stdout.write(self.style.SUCCESS("Done."))

    @staticmethod
    def _order(
        name, phone, address, lines, *, status, created_at,
        latitude=STORE_LATITUDE, longitude=STORE_LONGITUDE,
        rider=None, delivery_type=None,
    ) -> Order:
        """Create one order with totals computed the way checkout computes them."""
        tier = resolve_tier(delivery_type)
        items_total = money(sum(product.price * quantity for product, quantity in lines))
        charges = compute_charges(items_total, tier.key)

        order = Order.objects.create(
            customer_name=name,
            customer_phone=phone,
            customer_address=address,
            customer_latitude=latitude,
            customer_longitude=longitude,
            status=status,
            created_at=created_at,
            delivery_boy=rider,
            # Both taken from the tier, the way checkout takes them, so a seeded
            # order's countdown agrees with the fee it was charged.
            delivery_type=tier.key,
            promised_minutes=tier.promise_minutes,
            # Stamped so the timeline on the tracking page is not blank for
            # states that are supposed to have already passed through it.
            packed_at=created_at if status in (Order.READY, Order.DISPATCHED, Order.DELIVERED) else None,
            dispatched_at=created_at if status in (Order.DISPATCHED, Order.DELIVERED) else None,
            delivered_at=created_at if status == Order.DELIVERED else None,
            **charges.as_dict(),
        )

        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                product=product,
                quantity=quantity,
                name=product.name,
                price=product.price,
                mrp=product.mrp,
                unit=product.unit,
                image_url=product.image_url,
                line_total=money(product.price * quantity),
            )
            for product, quantity in lines
        ])
        return order
