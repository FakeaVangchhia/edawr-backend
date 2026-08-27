"""Shared fixtures for the API tests.

Two things every test in this package depends on:

**The cache is cleared between tests.** DRF keeps throttle counters there, and
the cache is *not* part of the transaction Django rolls back after each test.
Without this, the twentieth test to call the login endpoint gets a 429 for
reasons that have nothing to do with what it is testing — and the failure moves
around as tests are reordered, which is the worst kind of flake to chase.

**Money is compared as Decimal, never float.** `assertEqual(response["price"],
62.1)` passes or fails depending on the JSON parser's rounding. The helpers here
return Decimals so the assertions mean what they say.

**The store is opened around the clock.** `StoreSettings` defaults to 07:00-22:00
in the store's timezone, which is right for a real shop and fatal for a test
suite: every checkout test would pass in the afternoon and fail after ten at
night, and the failure would look like a checkout bug. Tests that care about
opening hours set them explicitly; see `ClosedStoreTests`.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from api.models import (
    STORE_LATITUDE,
    STORE_LONGITUDE,
    AdminUser,
    Category,
    Customer,
    Order,
    Product,
    StoreSettings,
    User,
)
from api.security import ADMIN_TOKEN, CUSTOMER_TOKEN, RIDER_TOKEN, create_access_token
from api.security import hash_password

ADMIN_EMAIL = "admin@edawr.test"
MANAGER_EMAIL = "manager@edawr.test"
ADMIN_PASSWORD = "admin-password-1"
RIDER_PIN = "4813"
# Deliberately not all digits and not the customer's own number: those are
# exactly what `validate_password_strength` refuses, and a fixture that could
# not be set through the API would be testing a state the app cannot reach.
CUSTOMER_PHONE = "+919000000101"
CUSTOMER_PASSWORD = "basket-of-milk-7"


class APIFixtures:
    """Everything a test needs to build a store, split out from the base class.

    A mixin rather than a base class so the same fixtures serve two different
    Django test semantics. `APITestBase` below wraps each test in a transaction
    and rolls it back, which is fast and is what almost every test wants.
    `APITransactionTestBase` commits instead, which is slower and truncates the
    tables afterwards — and is the only way to write a concurrency test, because
    a second thread opens its own connection and cannot see the first one's
    uncommitted rows. A test of `select_for_update()` written against the
    rolled-back base silently tests nothing at all.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.open_store()

    @staticmethod
    def store_timezone():
        """The zone the store trades in, for tests that reason about wall clock."""
        from zoneinfo import ZoneInfo

        from django.conf import settings as django_settings

        return ZoneInfo(django_settings.STORE_TIMEZONE)

    @staticmethod
    def open_store(**overrides) -> None:
        """The singleton, trading around the clock unless told otherwise.

        `opens_at == closes_at` is how the model spells "always open", so this
        is one row write rather than a patched clock.

        **One UPDATE, deliberately.** This runs in `setUp` for every test in the
        suite, and the obvious `load()` / mutate / `save()` shape costs a SELECT,
        an INSERT-or-nothing and an UPDATE each time. Against Postgres over TCP
        that was about a thousand extra round trips and it doubled the suite's
        runtime — from ~10s to ~18s — which is the sort of tax that quietly
        stops people running the tests. Migration 0007 creates the row, and each
        test runs inside a transaction that is rolled back, so it is always
        there and `filter().update()` is enough.
        """
        from datetime import time

        # Merged rather than splatted alongside: an override naming one of the
        # three defaults is the normal case here, and `f(a=1, **{"a": 2})` is a
        # TypeError, not a last-one-wins.
        fields = {
            "is_accepting_orders": True,
            "opens_at": time(0, 0),
            "closes_at": time(0, 0),
            **overrides,
        }
        StoreSettings.objects.filter(pk=1).update(**fields)

    def tearDown(self):
        cache.clear()
        super().tearDown()

    # --- actors ----------------------------------------------------------
    def make_admin(
        self,
        email: str = ADMIN_EMAIL,
        active: bool = True,
        role: str = AdminUser.ADMIN,
    ) -> AdminUser:
        """Idempotent, because tests call `as_admin()` more than once.

        A test that places an order (anonymous) and then inspects it as an admin
        switches credentials twice; creating a second row with the same email
        would blow up on the unique constraint for a reason that has nothing to
        do with what is being tested.

        Defaults to the ADMIN role so every test written before roles existed
        keeps meaning what it meant: full console access.
        """
        admin, _ = AdminUser.objects.get_or_create(
            email=email,
            defaults={
                "password_hash": hash_password(ADMIN_PASSWORD),
                "is_active": active,
                "role": role,
            },
        )
        return admin

    def make_manager(self, email: str = MANAGER_EMAIL, active: bool = True) -> AdminUser:
        """A console account with the MANAGER role — runs the store, cannot
        create accounts or read the audit log."""
        return self.make_admin(email=email, active=active, role=AdminUser.MANAGER)

    def make_rider(
        self,
        name: str = "Rider One",
        phone: str = "+919000000002",
        *,
        active: bool = True,
        available: bool = True,
        latitude: float = 23.7272,
        longitude: float = 92.7178,
        radius: float = 10.0,
    ) -> User:
        return User.objects.create(
            name=name,
            role=User.DELIVERY,
            phone=phone,
            pin_hash=hash_password(RIDER_PIN),
            is_active=active,
            is_available=available,
            base_latitude=latitude,
            base_longitude=longitude,
            service_radius_km=radius,
        )

    def make_customer(
        self,
        phone: str = CUSTOMER_PHONE,
        *,
        active: bool = True,
        verified: bool = False,
        name: str = "Customer One",
    ) -> Customer:
        """A shopper's account.

        `verified=False` is the default because it is the only state the
        deployed app can currently produce — nothing writes `phone_verified_at`
        until there is an SMS provider. A test that wants the verified branch
        has to ask for it, which keeps the unverified path the one being
        exercised by accident rather than the other way round.
        """
        return Customer.objects.create(
            phone=phone,
            password_hash=hash_password(CUSTOMER_PASSWORD),
            name=name,
            is_active=active,
            phone_verified_at=timezone.now() if verified else None,
        )

    # --- credentials -----------------------------------------------------
    def as_admin(self, admin: AdminUser | None = None) -> AdminUser:
        admin = admin or self.make_admin()
        token = create_access_token(
            admin.email, ADMIN_TOKEN, version=admin.token_version
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return admin

    def as_manager(self, manager: AdminUser | None = None) -> AdminUser:
        """Sign in as a Manager. Note the token is an ordinary *admin* token —
        the role lives on the row, not in the JWT, so there is nothing
        role-shaped to forge here."""
        return self.as_admin(manager or self.make_manager())

    def as_rider(self, rider: User | None = None) -> User:
        rider = rider or self.make_rider()
        token = create_access_token(
            rider.phone, RIDER_TOKEN, version=rider.token_version
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return rider

    def as_customer(self, customer: Customer | None = None) -> Customer:
        customer = customer or self.make_customer()
        token = create_access_token(
            customer.phone, CUSTOMER_TOKEN, version=customer.token_version
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return customer

    def as_anonymous(self) -> None:
        self.client.credentials()

    # --- catalogue -------------------------------------------------------
    def make_product(
        self,
        name: str = "Amul Taaza Milk",
        price: str = "62.00",
        *,
        mrp: str | None = None,
        stock: int = 20,
        category: str = "Dairy & Bread",
        status: str = Product.ACTIVE,
        **extra,
    ) -> Product:
        return Product.objects.create(
            name=name,
            price=Decimal(price),
            mrp=Decimal(mrp or price),
            cost_price=Decimal("40.00"),
            stock=stock,
            category=category,
            unit="1 L",
            status=status,
            **extra,
        )

    def make_category(self, name: str = "Dairy & Bread", **kwargs) -> Category:
        return Category.objects.create(name=name, **kwargs)

    # --- orders ----------------------------------------------------------
    def checkout_payload(self, product: Product, quantity: int = 2, **overrides) -> dict:
        """A checkout body a real client could have sent.

        Carries coordinates by default, because most fixtures are about
        something downstream of dispatch and dispatch needs a position to rank.
        A test about the *absence* of one passes
        `customer_latitude=None, customer_longitude=None` explicitly — which is
        the point of it being explicit: it used to be impossible to write, since
        the serializer defaulted the columns to the store's own coordinates and
        "no position" was unrepresentable.
        """
        payload = {
            "customer_name": "Lalrinsangi",
            "customer_phone": "9812345678",
            "customer_address": "House 42, Chanmari, Aizawl",
            "customer_latitude": STORE_LATITUDE,
            "customer_longitude": STORE_LONGITUDE,
            "items": [{"product_id": product.id, "quantity": quantity}],
        }
        payload.update(overrides)
        # None means "the customer shared nothing", which the serializer accepts
        # and the model stores as NULL. Sending the key with a null value and
        # omitting it entirely mean the same thing; drop it so both paths are
        # exercised identically.
        if payload.get("customer_latitude") is None:
            payload.pop("customer_latitude", None)
            payload.pop("customer_longitude", None)
        return payload

    def place_order(self, product: Product, quantity: int = 2, **overrides) -> Order:
        """Create an order through the real checkout endpoint.

        Going through HTTP rather than the ORM keeps the fixture honest: an
        order built by hand can hold a combination of fields checkout would
        never produce, and then a test passes against data that cannot exist.
        """
        self.as_anonymous()
        response = self.client.post(
            "/api/store/orders",
            self.checkout_payload(product, quantity, **overrides),
            format="json",
        )
        assert response.status_code == 201, response.data
        return Order.objects.get(pk=response.data["id"])

    def advance(self, order: Order, *statuses: str) -> Order:
        """Walk an order through the state machine without going via HTTP."""
        for status in statuses:
            changed = order.advance_status(status)
            order.save(update_fields=changed)
        order.refresh_from_db()
        return order

    # --- assertions ------------------------------------------------------
    def assertMoney(self, actual, expected: str, msg: str = ""):
        """Compare a JSON money value against an exact decimal string."""
        self.assertEqual(Decimal(str(actual)), Decimal(expected), msg)


class APITestBase(APIFixtures, APITestCase):
    """The default. One transaction per test, rolled back at the end."""


class APITransactionTestBase(APIFixtures, APITransactionTestCase):
    """For tests that need more than one connection to see each other's writes.

    Real commits and a table truncation between tests, so it is markedly slower
    — use it only where concurrency is the thing under test.

    `serialized_rollback` is off and `available_apps` unset, so Django truncates
    every table afterwards. That includes the `store_settings` row migration
    0007 created, which `open_store()` then updates to nothing. Recreating it in
    `setUp` keeps these tests independent of the truncation order.
    """

    def setUp(self):
        StoreSettings.objects.get_or_create(pk=1)
        super().setUp()
