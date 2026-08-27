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
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

# Every money column in this file. Declared once so a change to precision cannot
# be applied to four of the five places it matters.
MONEY = {"max_digits": 10, "decimal_places": 2}
ZERO = Decimal("0.00")

# Whether a catalogue row is for sale. Products and categories share one
# vocabulary because they mean the same thing by it — and because two identical
# choice sets under two names is a distinction with no difference that the
# schema generator then has to be told how to name.
ACTIVE = "active"
INACTIVE = "inactive"
STATUS_CHOICES = [(ACTIVE, "Active"), (INACTIVE, "Inactive")]

# The dark store's own position, and the fallback for a rider whose base has
# never been set. It was written out six times across models, serializers and
# the seed command, which is how `Order.customer_latitude` came to *default* to
# it — see the note on that field for what that cost.
STORE_LATITUDE = 23.7272
STORE_LONGITUDE = 92.7178


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

    ADMIN = "admin"
    MANAGER = "manager"
    ROLE_CHOICES = [(ADMIN, "Admin"), (MANAGER, "Manager")]

    email = models.EmailField(unique=True, db_index=True)
    password_hash = models.CharField(max_length=255)
    # Display name for the console header and the audit trail. Blank rather than
    # null: one empty spelling is enough, and `""` needs no None-check at render.
    name = models.CharField(max_length=120, blank=True, default="")

    # Which console this account may operate. Both roles run the store; only
    # ADMIN may mint or re-role other console accounts and read the audit log.
    #
    # The role is deliberately *not* a JWT claim. `AdminJWTAuthentication`
    # re-reads this row on every request, which is exactly why `is_active` works
    # as immediate revocation — a demoted manager loses access on their next
    # request rather than whenever their 12-hour token happens to expire. Putting
    # the role in the token would trade that for a stale copy.
    #
    # The default is MANAGER because the safe failure for a row created without
    # an explicit role is the *lesser* privilege. Migration 0005 backfills the
    # accounts that predate this column to ADMIN instead: they were already
    # all-powerful, and silently demoting them would lock the owner out.
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=MANAGER)

    is_active = models.BooleanField(default=True)

    # Bumped to invalidate every token this account currently holds.
    #
    # A JWT is a bearer credential the server does not store, so the only way
    # to retire one before it expires is to make the signature stop matching
    # something the server *does* store. `create_access_token` copies this
    # number into a `ver` claim and the authentication class compares it to
    # this column on every request — the same re-read that makes `is_active` an
    # immediate revocation, so the cost is already paid.
    #
    # It is per-account, not per-token, so signing out retires the credential
    # on every device rather than just the one in your hand. That is the right
    # default here (one console per person, one phone per rider), and the
    # alternative — a `jti` blacklist — needs a store that outlives the token.
    token_version = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    last_login_at = models.DateTimeField(null=True, blank=True)

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

    @property
    def is_owner_admin(self) -> bool:
        """True for the ADMIN role. Read by `IsOwnerAdmin` and by the serializer.

        A property rather than a scattered `role == "admin"` comparison, so the
        string exists in one place and a future third role changes one line.
        """
        return self.role == self.ADMIN


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

    # Same mechanism as AdminUser.token_version — see the note there. A rider
    # who signs out on a phone they are handing back should not leave a working
    # twelve-hour credential on it.
    token_version = models.PositiveIntegerField(default=0)

    base_latitude = models.FloatField(default=STORE_LATITUDE)
    base_longitude = models.FloatField(default=STORE_LONGITUDE)
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


class Customer(models.Model):
    """A shopper's account. The third and last identity in this schema.

    Separate from `AdminUser` (console logins) and `User` (staff and riders)
    for the reason those two are separate from each other: they are different
    populations with different lifecycles, and merging them means one table
    where half the columns are NULL for half the rows. A customer has no role,
    no shift, and no PIN.

    **A rider who shops at the store they deliver for has two rows in two
    tables with two independent passwords.** That is correct — the accounts are
    revoked, priced and audited differently — and it is confusing enough to be
    worth writing down. It also means a phone number is unique *within* this
    table, not across the schema, so `users.phone` and `customers.phone` can
    hold the same number. `api/security.py`'s `typ` claim is what keeps the two
    tokens apart; see the note there.

    The account is **optional**. Guest checkout is not a fallback path here, it
    is the main one: an order carries `customer` NULL unless someone was signed
    in when they placed it, and everything downstream of `Order` works the same
    either way.
    """

    # The identity, stored as `+91XXXXXXXXXX` by `api/validators.normalise_phone`
    # — the same normalisation checkout already applies, so the number someone
    # types at checkout and the number they sign in with are one string. Two
    # spellings of one number would be two accounts.
    phone = models.CharField(max_length=32, unique=True)

    # Not nullable, unlike `User.pin_hash`. A staff row exists because someone
    # works here and may or may not be able to sign in; a customer row exists
    # only because someone set a password, so there is no such thing as one
    # without a credential.
    password_hash = models.CharField(max_length=255)

    # Blank rather than null, matching AdminUser.name: one empty spelling, and
    # `""` needs no None-check at render.
    name = models.CharField(max_length=120, blank=True, default="")

    # When this person proved they hold the SIM — and NULL until they have.
    #
    # **This is the seam the whole verification design hangs on.** Setting a
    # password proves someone knows a number, not that they own it. So an
    # unverified account sees only the orders placed while signed in to it;
    # older guest orders carrying the same number stay hidden, because
    # otherwise typing a stranger's number into the sign-up form would hand
    # over that stranger's name, address and order history. The gate is in
    # `api/views/customer.py` and reads this column directly.
    #
    # It is a timestamp rather than a boolean so that "when" is answerable
    # later without a migration, and every gate in the codebase tests it with
    # `IS NULL` rather than joining anything.
    #
    # Nothing writes it yet: sending a code needs an SMS provider, which needs
    # DLT registration (see deployment.md, Known gaps). When that lands, the OTP
    # view's entire job is to stamp this field — no schema change, because the
    # queries and the serializers already read it.
    #
    # **Keep the challenge itself stateless when you build it** — a signed,
    # expiring token over the phone number (`django.core.signing.TimestampSigner`)
    # or a cache key with a TTL. Storing the sent code would mean columns here,
    # which would make this comment's promise false.
    phone_verified_at = models.DateTimeField(null=True, blank=True)

    # Re-read by CustomerJWTAuthentication on every request, so deactivating an
    # account revokes it immediately rather than when the token expires. Same
    # bargain as AdminUser.is_active and User.is_active.
    is_active = models.BooleanField(default=True)

    # Same mechanism as the other two identities — see the note on
    # AdminUser.token_version. Bumped by sign-out and by a password change.
    token_version = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "customers"

    def __str__(self) -> str:
        return self.name or self.phone

    @property
    def is_authenticated(self) -> bool:
        """DRF reads this off `request.user`; see AdminUser for the reasoning.

        **Returning True here is what makes `api/throttling.CustomerRateThrottle`
        necessary.** DRF's `AnonRateThrottle` returns no key — and therefore no
        limit — the moment a request is authenticated, and `StaffRateThrottle`
        returns none for anything that is not staff. Without a throttle class
        that recognises this model, a signed-in customer would fall through both
        and reach every endpoint completely unmetered.
        """
        return True


class Category(models.Model):
    # Re-exported so `Category.ACTIVE` keeps working alongside `Product.ACTIVE`.
    ACTIVE = ACTIVE
    INACTIVE = INACTIVE
    STATUS_CHOICES = STATUS_CHOICES

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
    # `choices` is enforced by the serializer, not by SQLite. Before it existed
    # this was free text, so "actve" silently withdrew a category from the rail
    # and looked like a data-loss bug rather than a typo.
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=ACTIVE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "categories"
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    ACTIVE = ACTIVE
    INACTIVE = INACTIVE
    STATUS_CHOICES = STATUS_CHOICES

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
    # See Category.status: validated by the serializer, so a misspelling is a
    # 400 rather than a product that quietly stops being for sale.
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=ACTIVE, db_index=True
    )

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
    FAILED = "Failed"

    STATUS_CHOICES = [
        (PLACED, "Placed"),
        (PACKING, "Packing"),
        (READY, "Ready for pickup"),
        (DISPATCHED, "Out for delivery"),
        (DELIVERED, "Delivered"),
        (CANCELLED, "Cancelled"),
        (FAILED, "Delivery failed"),
    ]

    # The only legal moves. Anything not listed here raises, which is what stops
    # a stale mobile screen from marking a cancelled order Delivered.
    #
    #   Placed -> Packing -> Ready -> Dispatched -> Delivered
    #      \________\_________\____________________> Cancelled
    #
    # Dispatched -> Ready exists so a rider who cannot complete a delivery can
    # hand it back to the pool instead of stranding it.
    #   Dispatched -> Failed  is the door the store did not have. Until it
    #   existed the only recorded outcome of a dispatched order was Delivered,
    #   so a customer who refused the bag, an address nobody answered, or a
    #   stolen bike all had to be filed as a completed sale: the goods were
    #   recorded as sold and paid for, and the stock never came back. Every
    #   actor's only button was "Mark delivered", which is a lie the till later
    #   has to absorb.
    #
    #   It is terminal and it does *not* restock by itself, because the bag is
    #   wherever the rider is. `checkout.restock_failed_order()` is what returns
    #   the units, and a manager calls it when the goods are physically back on
    #   the shelf.
    TRANSITIONS: dict[str, tuple[str, ...]] = {
        PLACED: (PACKING, CANCELLED),
        PACKING: (READY, CANCELLED),
        READY: (DISPATCHED, PACKING, CANCELLED),
        DISPATCHED: (DELIVERED, READY, FAILED),
        DELIVERED: (),
        CANCELLED: (),
        FAILED: (),
    }

    # Statuses where the goods have not left the store, so cancelling can safely
    # put the stock back.
    CANCELLABLE = (PLACED, PACKING, READY)
    # Statuses that are over. Used to keep finished orders out of live feeds.
    TERMINAL = (DELIVERED, CANCELLED, FAILED)

    COD = "cod"
    PAYMENT_CHOICES = [(COD, "Cash on delivery")]

    # How fast the customer asked for it, and therefore what they were charged
    # and what they were promised. Stored as a word rather than a boolean
    # because a third tier is a plausible future and `is_express = False` would
    # then mean nothing in particular.
    INSTANT = "instant"
    SLOW = "slow"
    DELIVERY_TYPE_CHOICES = [
        (INSTANT, "Instant"),
        (SLOW, "Slow"),
    ]

    # --- who and where ---------------------------------------------------
    # The account that placed this, when there was one. NULL is the ordinary
    # case, not a degraded one: guest checkout is the main path and every order
    # placed before customer accounts existed carries NULL for ever.
    #
    # `SET_NULL` rather than `CASCADE` — deleting an account must not delete the
    # store's sales records. Same shape and same reasoning as `collected_by` and
    # `delivery_boy` below.
    #
    # **This never overwrites the three fields under it.** The FK says *which
    # account placed the order*; `customer_name` and `customer_phone` say *what
    # was typed at the time*, and they are not the same question — ordering for
    # your mother at her number is the normal case, and the rider dials what was
    # typed. Same reasoning as `OrderItem.name` and `OrderItem.price`.
    customer = models.ForeignKey(
        "Customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        db_column="customer_id",
    )
    customer_name = models.CharField(max_length=255)
    customer_phone = models.CharField(max_length=32, db_index=True)
    customer_address = models.CharField(max_length=500)
    customer_landmark = models.CharField(max_length=255, null=True, blank=True)
    delivery_notes = models.CharField(max_length=500, null=True, blank=True)
    # Nullable, and that is the whole point. These used to *default* to the
    # store's own coordinates, so an order that carried no position recorded the
    # customer as standing at the counter. Every rider was then 0.00 km away:
    # the service-radius filter matched everyone, "nearest first" degenerated to
    # ordering by id, and the rider app displayed a confident, false `0.0 km`.
    # NULL says "we do not know", which the dispatch code can then treat as the
    # different thing it is.
    customer_latitude = models.FloatField(null=True, blank=True)
    customer_longitude = models.FloatField(null=True, blank=True)

    # The customer's proof of ownership. Unguessable, so /api/store/orders/{token}
    # can be public and still show a name, phone number and address.
    tracking_token = models.CharField(
        max_length=64, unique=True, db_index=True, default=generate_tracking_token
    )

    # The client's own name for one checkout attempt, from the `Idempotency-Key`
    # header. Its whole job is this unique constraint.
    #
    # Checkout is a POST that writes an order, writes N item rows and moves
    # stock. A customer on Aizawl mobile data whose request times out after the
    # server committed will retry it — the browser may even retry it for them —
    # and without a key that second request is indistinguishable from a second
    # order. They are charged twice and the shelf is decremented twice.
    #
    # Nullable, because a client that sends no key must keep working exactly as
    # before, and because every row that predates this column has no key. Note
    # that Postgres treats NULLs as distinct in a unique index, so any number of
    # key-less orders coexist — which is the behaviour we want and not an
    # accident worth "fixing" with a sentinel value.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True, unique=True)

    # --- state -----------------------------------------------------------
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default=PLACED, db_index=True
    )
    # Carries the reason for both terminal-by-failure states. One column rather
    # than two because a manager reading the order wants "why did this not
    # arrive?" answered in one place, and the status already says which kind of
    # not-arriving it was.
    cancellation_reason = models.CharField(max_length=255, null=True, blank=True)
    # Set when the goods from a failed delivery are physically back on the
    # shelf. Distinct from the status: an order can be Failed for an hour before
    # the rider returns, and the stock must not reappear until it really has.
    restocked_at = models.DateTimeField(null=True, blank=True)

    # --- money (all computed server-side; the client never sends a total) --
    items_total = models.DecimalField(**MONEY, default=ZERO)
    delivery_fee = models.DecimalField(**MONEY, default=ZERO)
    handling_fee = models.DecimalField(**MONEY, default=ZERO)
    grand_total = models.DecimalField(**MONEY, default=ZERO)
    payment_method = models.CharField(max_length=32, choices=PAYMENT_CHOICES, default=COD)

    # --- money actually collected ----------------------------------------
    # `payment_method` records an *intention*: this order is to be paid in cash
    # at the door. Nothing recorded whether that ever happened, so at end of
    # shift the only way to answer "how much cash does this rider owe the till?"
    # was to sum `grand_total` over their delivered orders and trust it. For a
    # cash business that is the primary shrinkage vector, and trusting the
    # expected figure is exactly the thing a reconciliation exists to stop.
    #
    # All three are stamped by `advance_status()` on the move to Delivered, so
    # there is no route to Delivered that records nothing. `amount_collected`
    # defaults to `grand_total` there and the rider may revise it downward when
    # the customer paid short; the gap between the two columns is the shortfall
    # the console reports.
    #
    # Nullable rather than defaulting to zero: "not delivered" and "delivered,
    # collected nothing" are different facts, and a zero default would file
    # every open order as an unpaid one.
    paid_at = models.DateTimeField(null=True, blank=True)
    amount_collected = models.DecimalField(**MONEY, null=True, blank=True)
    # The rider who took the money. NULL when an admin closed the order from the
    # console — there was no rider at a door, and `AuditLog` records who did it.
    collected_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="collected_orders",
        db_column="collected_by_id",
    )

    # --- the delivery promise --------------------------------------------
    # Both snapshotted per order rather than read from settings at display
    # time, so re-tuning a tier never rewrites what an existing customer was
    # already told. `delivery_type` is the tier they chose; `promised_minutes`
    # is that tier's window as it stood the moment they ordered.
    delivery_type = models.CharField(
        max_length=16, choices=DELIVERY_TYPE_CHOICES, default=INSTANT
    )
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
    # `offered_to_delivery_boy` used to sit here. It was assigned None in five
    # places and a rider in none, which left four guards reading it permanently
    # inert -- including `delivery.py`'s `.filter(offered_to_delivery_boy__isnull
    # =True)`, which matched every row. It was the vestige of a push-dispatch
    # design `views/delivery.py` documents choosing against, and building the
    # feature it implied would need a scheduler to expire unanswered offers.
    # Removed rather than kept as scaffolding for a decision already made.
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

    # --- fulfilment outcome, stamped once at delivery --------------------
    # Both are derivable from the timestamps above, and both are stored anyway.
    # Two reasons, and the second is the important one:
    #
    # 1. Aggregating them is a plain AVG and COUNT over an indexed column. The
    #    alternative is interval arithmetic against `promised_minutes` inside
    #    every analytics query, which is exactly the sort of expression that
    #    behaves differently on SQLite and Postgres — and this project develops
    #    on one and ships on the other.
    # 2. **They record what was true at delivery.** `promised_minutes` is the
    #    tier's window as it stood when the order was placed, but "was this late"
    #    is a judgement made once, at the door. Recomputing it later means last
    #    quarter's on-time rate silently changes when someone edits a delivery
    #    tier. A business record has to stay put.
    #
    # NULL means "not delivered", which is why `was_late` is nullable rather than
    # defaulting to False — an undelivered order is not an on-time one.
    delivered_in_minutes = models.IntegerField(null=True, blank=True)
    was_late = models.BooleanField(null=True, blank=True)

    class Meta:
        db_table = "orders"
        ordering = ["-id"]
        indexes = [
            # The rider feed: open orders, newest first.
            models.Index(fields=["status", "created_at"], name="order_feed_idx"),
            # A signed-in customer's order history, newest first — the ordering
            # `GET /api/customer/orders` asks for, so the index answers the
            # query without a sort.
            models.Index(fields=["customer", "-created_at"], name="order_customer_idx"),
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
            # A failed delivery ends the order at the same point in the
            # lifecycle a cancellation does, and the analytics that read
            # `cancelled_at` mean "when did this stop being a sale".
            self.FAILED: "cancelled_at",
        }
        field = stamps.get(new_status)
        if field and getattr(self, field) is None:
            setattr(self, field, now)
            changed.append(field)

            # Freeze the fulfilment outcome at the same moment, and only then.
            # Guarded by the same "stamped only if empty" rule above, so an
            # order that bounces Dispatched -> Ready -> Delivered is still
            # measured from when it was placed, and re-delivering cannot
            # rewrite the number.
            if new_status == self.DELIVERED:
                elapsed = int((now - self.created_at).total_seconds() // 60)
                self.delivered_in_minutes = elapsed
                self.was_late = elapsed > self.promised_minutes
                changed += ["delivered_in_minutes", "was_late"]

                # Cash on delivery means the money and the goods change hands at
                # the same instant, so this is the moment the payment happened
                # and there is no later one to wait for.
                #
                # Stamped here rather than in the view on purpose. There are
                # several routes to Delivered — the rider app, the console, a
                # future one nobody has written — and a payment record that
                # depends on each of them remembering to write it is a payment
                # record with holes in it. `grand_total` is the default because
                # it is what the order is worth; `OrderStatusView` revises it
                # down when the rider says the customer paid short.
                self.paid_at = now
                self.amount_collected = self.grand_total
                changed += ["paid_at", "amount_collected"]

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


class RiderDevice(models.Model):
    """A phone we can wake up, and which rider is holding it.

    The rider app polls every fifteen seconds while it is *open*. That is the
    whole problem this table solves: dispatch assigns an order the instant a
    manager marks the bag packed, and the phone it lands on is in a pocket with
    the screen off. Nothing polls there. Before this, the rider found out when
    they next looked — which on a fifteen-minute promise is most of the promise.

    **The Expo token is the identity, not the rider.** `expo_token` is unique
    across the table rather than unique per rider, and registration moves an
    existing row to the caller instead of inserting a second one. A phone handed
    from one rider to the next at shift change would otherwise sit in two rows,
    and every order assigned to *either* rider would buzz on it. The person
    holding the handset is whoever signed in last, and the unique constraint is
    what makes that true by construction rather than by cleanup.

    **A rider may hold several rows.** One phone, one tablet, a spare handset —
    all legitimate, all notified. That is why this is a table and not a column
    on `User`.

    **Rows are deleted, never deactivated.** Expo answers a send with
    `DeviceNotRegistered` once an app is uninstalled or its token is rotated,
    and `api/push.py` deletes the row when it sees that. A dead token kept as
    `is_active=False` would be a row we re-try forever; there is nothing to
    audit here, because the record of who was notified is the order itself.
    """

    IOS = "ios"
    ANDROID = "android"
    UNKNOWN = ""
    PLATFORM_CHOICES = [(IOS, "iOS"), (ANDROID, "Android"), (UNKNOWN, "Unknown")]

    rider = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="devices", db_column="rider_id"
    )
    # `ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]` — around 41 characters today,
    # but it is an opaque string from someone else's service, so the column is
    # sized for it to grow rather than for what it happens to be.
    expo_token = models.CharField(max_length=255, unique=True)
    # Recorded for support ("it works on Android and not on my iPhone"), never
    # branched on: Expo's send API takes the same shape for both.
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES, blank=True, default=UNKNOWN)
    created_at = models.DateTimeField(default=timezone.now)
    # Bumped on every re-registration, which the app does on each launch. A row
    # that has not been seen in months is a phone that is gone.
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "rider_devices"
        indexes = [
            # The only query that matters: "every phone belonging to this rider".
            models.Index(fields=["rider"], name="device_rider_idx"),
        ]

    def __str__(self) -> str:
        return f"device for rider {self.rider_id}"


class AuditLog(models.Model):
    """Who changed what, and when.

    `Order` already carries five lifecycle timestamps, so the system could always
    say *when* an order was packed — but never by whom. With two console roles
    that is no longer acceptable: "the manager cancelled it" and "the owner
    cancelled it" are different facts, and a cash business needs to tell them
    apart after the argument, not during it.

    **Every column here is denormalised on purpose.** `actor_label` stores the
    email or name as it read at the time rather than a foreign key, because a log
    that breaks when an account is deleted is not a log — and `AdminUser` and
    `User` are two different tables anyway, so no single FK could point at both.
    `actor_kind` is what disambiguates their overlapping primary keys.

    Rows are written by `api/audit.py::record` and never updated. Nothing deletes
    them; if the table ever needs trimming that is a scheduled job, not a cascade.
    """

    ADMIN = "admin"
    RIDER = "rider"
    SYSTEM = "system"
    ACTOR_CHOICES = [(ADMIN, "Admin"), (RIDER, "Rider"), (SYSTEM, "System")]

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    STATUS = "status"
    ASSIGN = "assign"
    CANCEL = "cancel"
    ACTION_CHOICES = [
        (CREATE, "Create"), (UPDATE, "Update"), (DELETE, "Delete"),
        (LOGIN, "Login"), (STATUS, "Status"), (ASSIGN, "Assign"),
        (CANCEL, "Cancel"),
    ]

    actor_kind = models.CharField(max_length=16, choices=ACTOR_CHOICES, default=SYSTEM)
    # Not a ForeignKey — see the class docstring. Null for SYSTEM rows.
    actor_id = models.IntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=255, blank=True, default="")
    actor_role = models.CharField(max_length=16, blank=True, default="")

    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    # The table touched, as a plain lowercase word: "product", "order", "admin".
    entity = models.CharField(max_length=32)
    entity_id = models.IntegerField(null=True, blank=True)

    # One human-readable sentence, written at the call site where the context is
    # known. The console shows this column and nothing else in the common case.
    summary = models.CharField(max_length=255, blank=True, default="")
    # {field: [before, after]}. JSONField so a diff of any shape survives; the
    # writer is responsible for keeping secrets (password_hash, pin_hash) out.
    changes = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "audit_log"
        # Newest first is the only order this is ever read in.
        ordering = ["-created_at", "-id"]
        indexes = [
            # "everything that happened to product 41"
            models.Index(fields=["entity", "entity_id"], name="audit_entity_idx"),
            # "everything this manager did"
            models.Index(fields=["actor_kind", "actor_id"], name="audit_actor_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.actor_label} {self.action} {self.entity}#{self.entity_id}"


class StoreSettings(models.Model):
    """The knobs an operator has to be able to turn without a deploy.

    Everything else about this store's economics — fees, thresholds, delivery
    tiers — lives in environment variables, and that is right for it: those are
    pricing decisions, they change rarely, and a change wants review. The four
    things here are different in kind. They are *operational*, they change
    within a shift, and the person who needs to change them is standing behind
    the counter at the time:

      - **The shop is shut.** There were no opening hours at all. A 03:00 order
        was accepted, promised in fifteen minutes, and sat on a board nobody was
        watching until the customer rang.
      - **Stop taking orders now.** A stock-take, a power cut, a rider
        shortage — a kill switch is the difference between a bad hour and a
        hundred promises the store cannot keep. There was no way to pause
        checkout short of taking the API down.
      - **How far we deliver.** Checkout accepted any latitude in −90..90.
        An address in Mumbai was charged and given a 15-minute Aizawl promise,
        then never appeared in a rider's feed, so it surfaced only once `is_late`
        tripped it into the stalled queue — after the whole promise had elapsed.
      - **Where "here" is.** The radius has to be measured from somewhere, and
        hardcoding the store's coordinates in the check would mean a second
        deploy to move the shop.

    **A singleton, enforced by `pk=1`.** `load()` is the only way to read it and
    creates the row on first use, so there is exactly one and no view has to
    handle "not configured yet". A settings table with a variable number of rows
    is a settings table someone eventually gets two of.
    """

    # The pk is pinned rather than auto so `load()` is a get_or_create on a
    # known key rather than a `.first()` that quietly picks one of two rows.
    id = models.AutoField(primary_key=True)

    # --- the kill switch ---------------------------------------------------
    # Independent of the hours below: closing early is a decision, and a store
    # inside its opening hours can still be shut.
    is_accepting_orders = models.BooleanField(default=True)
    # Shown to the customer verbatim on the storefront when checkout is off, so
    # "back in 20 minutes" is possible and "Closed" is not the only message.
    closed_message = models.CharField(max_length=255, blank=True, default="")

    # --- opening hours -----------------------------------------------------
    # Local wall-clock in settings.STORE_TIMEZONE, not UTC. A shopkeeper setting
    # "we open at 7" means seven in Aizawl.
    #
    # `opens_at == closes_at` means open around the clock, which is the honest
    # reading of a zero-length window and the only sane way to express 24-hour
    # trading in two time columns.
    #
    # `time(7, 0)`, not the string `"07:00"`. Django does not run `to_python`
    # on a field default, so a string default survives into any instance built
    # in memory rather than loaded from the database — and `within_hours()`
    # then compares `str` to `datetime.time` and raises. The row always exists
    # in practice (migration 0007 creates it), which is exactly why this went
    # unnoticed: the only path that reaches it is `load()`'s fallback, and that
    # fallback exists so a database missing the row degrades instead of
    # crashing. With a string default it crashed harder — a 500 on every
    # `/api/store/config` and every checkout.
    opens_at = models.TimeField(default=time(7, 0))
    closes_at = models.TimeField(default=time(22, 0))

    # --- the delivery zone -------------------------------------------------
    delivery_radius_km = models.FloatField(
        default=8.0, validators=[MinValueValidator(0.1)]
    )
    store_latitude = models.FloatField(default=STORE_LATITUDE)
    store_longitude = models.FloatField(default=STORE_LONGITUDE)

    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "store_settings"
        verbose_name_plural = "store settings"

    def __str__(self) -> str:
        return "Store settings"

    @classmethod
    def load(cls) -> "StoreSettings":
        """The one row, created with defaults if this is the first read.

        A plain SELECT first, rather than `get_or_create` outright. This is on
        the checkout path and on every `/api/store/config`, and `get_or_create`
        opens a savepoint and issues an INSERT attempt even when the row is
        obviously there — real work on every request to handle a case that
        happens once in the life of a database, and which migration 0007
        already handles at deploy time.

        The `get_or_create` fallback stays for the database that somehow has no
        row: a store with no opening hours is better created than crashed on.
        """
        row = cls.objects.filter(pk=1).first()
        if row is not None:
            return row
        row, _ = cls.objects.get_or_create(pk=1)
        return row

    # --- derived state -----------------------------------------------------
    def within_hours(self, at: datetime | None = None) -> bool:
        """Is the wall clock inside the trading window right now?

        Handles a window that crosses midnight (`22:00`–`02:00`) by testing the
        union of the two halves rather than a single `<=` range, which would be
        empty for every overnight shop.
        """
        from zoneinfo import ZoneInfo

        from django.conf import settings as django_settings

        now = (at or timezone.now()).astimezone(
            ZoneInfo(django_settings.STORE_TIMEZONE)
        ).time()

        if self.opens_at == self.closes_at:
            return True
        if self.opens_at < self.closes_at:
            return self.opens_at <= now < self.closes_at
        return now >= self.opens_at or now < self.closes_at

    def is_open(self, at: datetime | None = None) -> bool:
        return self.is_accepting_orders and self.within_hours(at)

    def closed_reason(self, at: datetime | None = None) -> str:
        """Why the store will not take this order, in a sentence a customer reads.

        Returns "" when it is open, so the caller can treat this as the whole
        check rather than asking twice.
        """
        if self.is_open(at):
            return ""
        if not self.is_accepting_orders:
            return self.closed_message.strip() or (
                "We have paused new orders for a moment. Please try again shortly."
            )
        return (
            f"We are closed right now. Orders open at "
            f"{self.opens_at.strftime('%H:%M')}."
        )
