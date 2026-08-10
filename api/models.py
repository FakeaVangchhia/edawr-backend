"""Django ORM models — the tables.

A near-mechanical translation of the old SQLAlchemy `app/models.py`. The three
differences worth knowing before you read on:

1. **`db_table` is set explicitly.** Django would otherwise name these
   `api_adminuser`, `api_orderitem`, and so on. Pinning the names keeps the
   schema identical to the SQLAlchemy one (and to the original Supabase one).

2. **`on_delete` is mandatory and lives in Python.** SQLAlchemy's
   `ForeignKey(..., ondelete="RESTRICT")` emitted a database-level constraint;
   Django's `on_delete=models.PROTECT` is enforced by the ORM *before* it issues
   the DELETE, and raises `ProtectedError`. It also writes the matching database
   constraint. The behaviour you relied on is unchanged.

3. **Foreign key attributes come in pairs.** Declaring `parent` gives you both
   `category.parent` (the object, one query) and `category.parent_id` (the raw
   integer, free). The API speaks `parent_id`; the serializers map between them.

Money is still `FloatField` because SQLite has no decimal type. Switch to
`DecimalField(max_digits=10, decimal_places=2)` when you move to Postgres.
"""

from django.db import models
from django.utils import timezone


class AdminUser(models.Model):
    """Admin console logins.

    Separate from `User` (store staff / riders), which has no password and is
    operational data. Auth identities and staff records are different concerns.

    This is deliberately *not* Django's `contrib.auth` user: that model exists to
    support cookie sessions, the Django admin site and a permission framework,
    none of which this API uses. `password_hash` is still produced by Django's
    hashers, so you get PBKDF2 and the upgrade machinery without the extra apps.
    """

    email = models.EmailField(unique=True, db_index=True)
    password_hash = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "admin_users"

    def __str__(self) -> str:
        return self.email

    @property
    def is_authenticated(self) -> bool:
        """DRF checks this attribute on `request.user`.

        Django's own user model gets it from `AbstractBaseUser`; this model is
        standalone, so it declares it. Always True — an unauthenticated request
        has `request.user is None`, not an inactive AdminUser.
        """
        return True


class User(models.Model):
    """Store staff: managers and delivery riders."""

    MANAGER = "manager"
    DELIVERY = "delivery"
    ROLE_CHOICES = [(MANAGER, "Manager"), (DELIVERY, "Delivery")]

    name = models.CharField(max_length=255)
    # `choices` is validated by serializers and forms, not by SQLite. The
    # explicit role check in the create view is what actually returns the 400.
    role = models.CharField(max_length=32, choices=ROLE_CHOICES)
    phone = models.CharField(max_length=32, unique=True)
    # Riders sign in with phone + PIN, hashed by the same PBKDF2 hasher that
    # protects AdminUser.password_hash. NULL means "this user cannot sign in",
    # which is the correct default for managers and for every row that existed
    # before rider auth — a rider must be given a PIN explicitly.
    pin_hash = models.CharField(max_length=255, null=True, blank=True)
    base_latitude = models.FloatField(default=23.7272)
    base_longitude = models.FloatField(default=92.7178)
    service_radius_km = models.FloatField(default=10.0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "users"

    def __str__(self) -> str:
        return f"{self.name} ({self.role})"

    @property
    def is_authenticated(self) -> bool:
        """DRF reads this off `request.user`; see AdminUser for the reasoning."""
        return True


class Category(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    # "self" is a self-referential foreign key. `db_column` pins the column name
    # so it stays `parent_id` rather than Django's default `parent_id` for a
    # field named `parent` — which happens to match, but stating it removes the
    # coincidence.
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        db_column="parent_id",
    )
    status = models.CharField(max_length=32, default="active")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "categories"

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=64, null=True, blank=True)
    barcode = models.CharField(max_length=64, null=True, blank=True)
    # A free-text category label, not a foreign key to Category. That is how the
    # Supabase schema had it and the admin UI still edits it as a string.
    category = models.CharField(max_length=255, null=True, blank=True)
    brand = models.CharField(max_length=255, null=True, blank=True)
    unit = models.CharField(max_length=64, null=True, blank=True)
    price = models.FloatField(default=0.0)
    cost_price = models.FloatField(default=0.0)
    mrp = models.FloatField(default=0.0)
    stock = models.IntegerField(default=0)
    reorder_level = models.IntegerField(default=0)
    status = models.CharField(max_length=32, default="active")
    location = models.CharField(max_length=255, null=True, blank=True)
    supplier_name = models.CharField(max_length=255, null=True, blank=True)
    supplier_phone = models.CharField(max_length=32, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    # Stored as a *relative* path ("/uploads/foo.png") so the hostname is never
    # baked into the database. The frontend prefixes it with assetUrl().
    image_url = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "products"

    def __str__(self) -> str:
        return self.name


class Order(models.Model):
    PENDING = "Pending"
    ASSIGNED = "Assigned"
    DELIVERED = "Delivered"
    # Capitalised on purpose — the frontend and the Expo app compare these
    # strings literally.
    STATUS_CHOICES = [(PENDING, PENDING), (ASSIGNED, ASSIGNED), (DELIVERED, DELIVERED)]

    customer_phone = models.CharField(max_length=32)
    customer_name = models.CharField(max_length=255, default="Customer")
    customer_address = models.CharField(max_length=500, default="Bazar Bawn, Aizawl")
    customer_latitude = models.FloatField(default=23.7272)
    customer_longitude = models.FloatField(default=92.7178)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=PENDING)

    delivery_boy = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_orders",
        db_column="delivery_boy_id",
    )
    offered_to_delivery_boy = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="offered_orders",
        db_column="offered_to_delivery_boy_id",
    )
    offered_distance_km = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "orders"

    def __str__(self) -> str:
        return f"Order #{self.pk} ({self.status})"


class OrderItem(models.Model):
    # `related_name="items"` is what makes `order.items.all()` work and what the
    # OrderSerializer nests under the key the frontend expects.
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
        db_column="order_id",
    )
    # PROTECT, not CASCADE: deleting a product must never quietly erase line
    # items from past orders. `name` and `price` are denormalised onto this row
    # precisely so order history survives catalogue changes — cascading would
    # destroy the record anyway. The delete view turns the resulting
    # ProtectedError into a clear 409.
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="order_items",
        db_column="product_id",
    )
    quantity = models.IntegerField(default=1)
    name = models.CharField(max_length=255)
    price = models.FloatField(default=0.0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "order_items"

    def __str__(self) -> str:
        return f"{self.quantity} x {self.name}"
