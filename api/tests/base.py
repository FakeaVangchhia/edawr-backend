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
"""

from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache
from rest_framework.test import APITestCase

from api.models import AdminUser, Category, Order, Product, User
from api.security import ADMIN_TOKEN, RIDER_TOKEN, create_access_token
from api.security import hash_password

ADMIN_EMAIL = "admin@edawr.test"
MANAGER_EMAIL = "manager@edawr.test"
ADMIN_PASSWORD = "admin-password-1"
RIDER_PIN = "4813"


class APITestBase(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

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

    # --- credentials -----------------------------------------------------
    def as_admin(self, admin: AdminUser | None = None) -> AdminUser:
        admin = admin or self.make_admin()
        token = create_access_token(admin.email, ADMIN_TOKEN)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return admin

    def as_manager(self, manager: AdminUser | None = None) -> AdminUser:
        """Sign in as a Manager. Note the token is an ordinary *admin* token —
        the role lives on the row, not in the JWT, so there is nothing
        role-shaped to forge here."""
        return self.as_admin(manager or self.make_manager())

    def as_rider(self, rider: User | None = None) -> User:
        rider = rider or self.make_rider()
        token = create_access_token(rider.phone, RIDER_TOKEN)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return rider

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
        )

    def make_category(self, name: str = "Dairy & Bread", **kwargs) -> Category:
        return Category.objects.create(name=name, **kwargs)

    # --- orders ----------------------------------------------------------
    def checkout_payload(self, product: Product, quantity: int = 2, **overrides) -> dict:
        payload = {
            "customer_name": "Lalrinsangi",
            "customer_phone": "9812345678",
            "customer_address": "House 42, Chanmari, Aizawl",
            "items": [{"product_id": product.id, "quantity": quantity}],
        }
        payload.update(overrides)
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
