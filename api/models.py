"""Django ORM models — the tables.

Three things here are load-bearing and worth reading before you change anything.

**1. Money is `DecimalField`, never float.** A float cannot represent 0.1, so
totalling a basket in floats drifts: 62.10 + 35.30 + 45.60 is not 143.00. That
is a rounding error you hand to a customer on a bill. Every price, fee and total
below is `Decimal(max_digits=10, decimal_places=2)`, and the arithmetic that
combines them lives in `pricing.py` where it is quantised explicitly. SQLite has
no decimal type and stores these as strings, which is fine — Django converts on
the way in and out, and Postgres has a real `numeric` when you move.

**2. Order status is a state machine, not a label.** The legal transitions are
declared in `Order.TRANSITIONS` and enforced by `Order.assert_can_transition`.
Nothing anywhere assigns `order.status` without going through it. A status field
that any view may set to any value is how orders end up Delivered before they
were ever packed.

**3. `db_table` is pinned on every model.** Django would otherwise name these
`api_order`, `api_orderitem` and so on. The explicit names keep the schema
identical to the SQLAlchemy one it was ported from (and the Supabase one before
that), so the tables you already have keep working.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

# Every money column in this file. Declared once so a change to precision cannot
# be applied to four of the five places it matters.
MONEY = {"max_digits": 10, "decimal_places": 2}
ZERO = Decimal("0.00")


def generate_tracking_token() -> str:
    """An unguessable handle for one order.

    Order tracking is public — a customer has no account, so the only thing that
    proves they own an order is possession of this token. It must therefore be
    long enough that walking the space is hopeless: 32 url-safe characters is
    ~190 bits. Sequential ids in the URL would let anyone read every customer's
    name, phone number and address by counting.
    """
    return secrets.token_urlsafe(24)


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
    # explicit role check in the serializer is what actually returns the 400.
    role = models.CharField(max_length=32, choices=ROLE_CHOICES)
    phone = models.CharField(max_length=32, unique=True)
    # Riders sign in with phone + PIN, hashed by the same PBKDF2 hasher that
    # protects AdminUser.password_hash. NULL means "this user cannot sign in",
    # which is the correct default for managers and for every row that existed
    # before rider auth — a rider must be given a PIN explicitly.
    pin_hash = models.CharField(max_length=255, null=True, blank=True)

    # Set False when someone leaves. Checked by RiderJWTAuthentication on every
    # request, so revoking access does not wait for their token to expire —
    # without it, a rider dismissed on Monday keeps delivering until Tuesday.
    is_active = models.BooleanField(default=True)
    # The rider's own on/off switch, toggled from the app. Distinct from
    # is_active: "not working right now" is the rider's call, "no longer works
    # here" is the manager's. Only riders who are both get offered orders.
    is_available = models.BooleanField(default=True)

    base_latitude = models.FloatField(default=23.7272)
    base_longitude = models.FloatField(default=92.7178)
    service_radius_km = models.FloatField(default=10.0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "users"
        indexes = [
            # The dispatch query filters on exactly this triple.
            models.Index(fields=["role", "is_active", "is_available"], name="user_dispatch_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.role})"

    @property
    def is_authenticated(self) -> bool:
        """DRF reads this off `request.user`; see AdminUser for the reasoning."""
        return True


class Category(models.Model):
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(null=True, blank=True)
    # "self" is a self-referential foreign key. `db_column` pins the column name
    # so it stays `parent_id`.
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        db_column="parent_id",
    )
    # Drives the category rail on the storefront. Relative path like a product
    # image ("/uploads/foo.png"); the frontend prefixes it via assetUrl().
    image_url = models.CharField(max_length=500, null=True, blank=True)
    # Controls left-to-right order in that rail. Ties break by name.
    sort_order = models.IntegerField(default=0)
    status = models.CharField(max_length=32, default="active")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "categories"
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    ACTIVE = "active"
    INACTIVE = "inactive"

    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=64, null=True, blank=True)
    barcode = models.CharField(max_length=64, null=True, blank=True)
    # A free-text category label, not a foreign key to Category. That is how the
    # Supabase schema had it and the admin UI still edits it as a string; the
    # storefront joins the two by name to pick up Category.image_url.
    category = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    brand = models.CharField(max_length=255, null=True, blank=True)
    unit = models.CharField(max_length=64, null=True, blank=True)

    # What the customer pays.
    price = models.DecimalField(**MONEY, default=ZERO, validators=[MinValueValidator(ZERO)])
    # What the store paid. Never exposed on the storefront — see ProductSerializer
    # vs StoreProductSerializer, which is why those are two different classes.
    cost_price = models.DecimalField(**MONEY, default=ZERO, validators=[MinValueValidator(ZERO)])
    # Printed price. When it is above `price` the storefront shows a struck-out
    # MRP and a discount badge, which is the whole visual language of this kind
    # of store. When it is not, no badge is shown rather than a fake one.
    mrp = models.DecimalField(**MONEY, default=ZERO, validators=[MinValueValidator(ZERO)])

    stock = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    reorder_level = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    status = models.CharField(max_length=32, default=ACTIVE, db_index=True)

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
        indexes = [
            # The storefront's default query: active products in one category.
            models.Index(fields=["status", "category"], name="product_store_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_in_stock(self) -> bool:
        return self.stock > 0

    @property
    def discount_percent(self) -> int:
        """Whole-percent saving off MRP, or 0 when there is no genuine saving.

        Rounded down so the badge can never overstate the discount.
        """
        if self.mrp <= ZERO or self.mrp <= self.price:
            return 0
        return int((self.mrp - self.price) / self.mrp * 100)


class Order(models.Model):
    """One customer order, from placement to doorstep.

    The status values are capitalised strings rather than integers because the
    web app, the rider app and the API docs all display them directly, and a
    human reading the database should not need a lookup table.
    """

    PLACED = "Placed"
    PACKING = "Packing"
    READY = "Ready"
    DISPATCHED = "Dispatched"
    DELIVERED = "Delivered"
    CANCELLED = "Cancelled"

    STATUS_CHOICES = [
        (PLACED, "Placed"),
        (PACKING, "Packing"),
        (READY, "Ready for pickup"),
        (DISPATCHED, "Out for delivery"),
        (DELIVERED, "Delivered"),
        (CANCELLED, "Cancelled"),
    ]

    # The only legal moves. Anything not listed here raises, which is what stops
    # a stale mobile screen from marking a cancelled order Delivered.
    #
    #   Placed -> Packing -> Ready -> Dispatched -> Delivered
    #      \________\_________\____________________> Cancelled
    #
    # Dispatched -> Ready exists so a rider who cannot complete a delivery can
    # hand it back to the pool instead of stranding it.
    TRANSITIONS: dict[str, tuple[str, ...]] = {
        PLACED: (PACKING, CANCELLED),
        PACKING: (READY, CANCELLED),
        READY: (DISPATCHED, PACKING, CANCELLED),
        DISPATCHED: (DELIVERED, READY),
        DELIVERED: (),
        CANCELLED: (),
    }

    # Statuses where the goods have not left the store, so cancelling can safely
    # put the stock back.
    CANCELLABLE = (PLACED, PACKING, READY)
    # Statuses that are over. Used to keep finished orders out of live feeds.
    TERMINAL = (DELIVERED, CANCELLED)

    COD = "cod"
    PAYMENT_CHOICES = [(COD, "Cash on delivery")]

    # --- who and where ---------------------------------------------------
    customer_name = models.CharField(max_length=255)
    customer_phone = models.CharField(max_length=32, db_index=True)
    customer_address = models.CharField(max_length=500)
    customer_landmark = models.CharField(max_length=255, null=True, blank=True)
    delivery_notes = models.CharField(max_length=500, null=True, blank=True)
    customer_latitude = models.FloatField(default=23.7272)
    customer_longitude = models.FloatField(default=92.7178)

    # The customer's proof of ownership. Unguessable, so /api/store/orders/{token}
    # can be public and still show a name, phone number and address.
    tracking_token = models.CharField(
        max_length=64, unique=True, db_index=True, default=generate_tracking_token
    )

    # --- state -----------------------------------------------------------
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=PLACED, db_index=True
    )
    cancellation_reason = models.CharField(max_length=255, null=True, blank=True)

    # --- money (all computed server-side; the client never sends a total) --
    items_total = models.DecimalField(**MONEY, default=ZERO)
    delivery_fee = models.DecimalField(**MONEY, default=ZERO)
    handling_fee = models.DecimalField(**MONEY, default=ZERO)
    grand_total = models.DecimalField(**MONEY, default=ZERO)
    payment_method = models.CharField(max_length=32, choices=PAYMENT_CHOICES, default=COD)

    # --- the delivery promise --------------------------------------------
    # Snapshotted per order rather than read from settings at display time, so
    # changing the store-wide promise never rewrites what an existing customer
    # was already told.
    promised_minutes = models.IntegerField(default=15)

    # --- fulfilment ------------------------------------------------------
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

    # --- lifecycle timestamps --------------------------------------------
    # Each is stamped once, by advance_status(). Together they are the audit
    # trail: how long picking took, how long the rider took, whether the
    # promise was met.
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    packed_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "orders"
        ordering = ["-id"]
        indexes = [
            # The rider feed: open orders, newest first.
            models.Index(fields=["status", "created_at"], name="order_feed_idx"),
        ]

    def __str__(self) -> str:
        return f"Order #{self.pk} ({self.status})"

    # --- the promise -----------------------------------------------------
    @property
    def promised_at(self) -> datetime:
        """The wall-clock time this order was promised for."""
        return self.created_at + timedelta(minutes=self.promised_minutes)

    @property
    def minutes_remaining(self) -> int:
        """Whole minutes left on the promise; 0 once it has run out.

        Rounded *up*, so 30 seconds left reads as "1 min" rather than "0 min" —
        a countdown that sits on zero while the rider is still coming reads as
        a broken app.
        """
        if self.status in self.TERMINAL:
            return 0
        remaining = (self.promised_at - timezone.now()).total_seconds()
        if remaining <= 0:
            return 0
        return int(-(-remaining // 60))

    @property
    def is_late(self) -> bool:
        return self.status not in self.TERMINAL and timezone.now() > self.promised_at

    @property
    def fulfilment_minutes(self) -> int | None:
        """How long the order actually took, once delivered."""
        if self.delivered_at is None:
            return None
        return int((self.delivered_at - self.created_at).total_seconds() // 60)

    # --- the state machine -----------------------------------------------
    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.TRANSITIONS.get(self.status, ())

    def advance_status(self, new_status: str) -> list[str]:
        """Move to `new_status`, stamping the matching timestamp.

        Returns the list of changed field names so the caller can pass it to
        `save(update_fields=...)` — a targeted UPDATE that cannot clobber a
        column some other request wrote in the meantime.

        Raises ValueError on an illegal move. Callers turn that into a 409;
        it is a conflict with the order's current state, not a bad request.
        """
        if new_status == self.status:
            return []
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Cannot move an order from {self.status} to {new_status}."
            )

        self.status = new_status
        changed = ["status"]
        now = timezone.now()

        # Stamped only if empty: an order that goes Dispatched -> Ready ->
        # Dispatched again keeps the moment it *first* left the store, which is
        # what the fulfilment numbers should measure.
        stamps = {
            self.READY: "packed_at",
            self.DISPATCHED: "dispatched_at",
            self.DELIVERED: "delivered_at",
            self.CANCELLED: "cancelled_at",
        }
        field = stamps.get(new_status)
        if field and getattr(self, field) is None:
            setattr(self, field, now)
            changed.append(field)

        return changed


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
    quantity = models.IntegerField(default=1, validators=[MinValueValidator(1)])

    # Copied from the product at checkout, never read back through the FK. A
    # price change tomorrow must not restate what someone was charged today.
    name = models.CharField(max_length=255)
    price = models.DecimalField(**MONEY, default=ZERO)
    mrp = models.DecimalField(**MONEY, default=ZERO)
    unit = models.CharField(max_length=64, null=True, blank=True)
    image_url = models.CharField(max_length=500, null=True, blank=True)
    # quantity * price, stored rather than derived. It is a financial record;
    # recomputing it later from columns that may since have been migrated is how
    # invoices stop reconciling.
    line_total = models.DecimalField(**MONEY, default=ZERO)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "order_items"

    def __str__(self) -> str:
        return f"{self.quantity} x {self.name}"


class OrderRejection(models.Model):
    """A rider declined this order; stop offering it to them.

    This is what makes the reject button mean something. The alternative design
    was a dispatch step that offers an order to one rider at a time and moves on
    after a timeout — that needs a scheduler and a background worker. Recording
    the decline instead keeps dispatch a pull: every available rider sees every
    nearby Ready order *except* the ones they personally turned down.

    The trade-off is that an order rejected by every rider in range silently
    stops appearing anywhere, so `GET /api/orders?stalled=true` exists to show a
    manager exactly that set.
    """

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="rejections", db_column="order_id"
    )
    rider = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="rejections", db_column="rider_id"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "order_rejections"
        constraints = [
            # Tapping reject twice is not an error, but it must not write two
            # rows — the view relies on get_or_create being idempotent.
            models.UniqueConstraint(
                fields=["order", "rider"], name="unique_order_rejection"
            )
        ]
        indexes = [
            models.Index(fields=["rider", "order"], name="rejection_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"order {self.order_id} rejected by rider {self.rider_id}"
