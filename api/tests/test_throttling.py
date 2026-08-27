"""That every kind of caller is metered by exactly one throttle.

The other suites test the limits that guard a particular endpoint — the login
budget in `test_auth.py`, the checkout budget in `test_checkout.py`. This file
tests something they cannot, because it is not a property of any endpoint: that
the *default throttle class list* leaves nobody uncovered.

**It exists because that has now failed twice, the same way both times.** DRF's
`AnonRateThrottle` stops metering a request the moment it is authenticated, and
each per-account class returns no key for a caller it does not recognise. So the
list covers everyone only for as long as the set of identities matches the set
of classes, and adding an identity is silent: nothing raises, no test that names
an endpoint goes red, the limits simply cease to exist for the new caller.

The first time, it was a staff token. The second, a customer's.
"""

from unittest.mock import patch

from rest_framework.throttling import SimpleRateThrottle

from api.models import AdminUser
from api.security import hash_password
from api.tests.base import ADMIN_PASSWORD, APITestBase

# Public, cheap, and reachable by absolutely everyone — which is the point. A
# customer-only endpoint would prove only that customer endpoints are metered,
# and the hole was never there.
PUBLIC_URL = "/api/store/products"


def with_throttle_rates(**rates):
    """Temporarily change individual throttle scopes.

    Duplicated from `test_checkout.py` rather than imported, so this file does
    not depend on a feature suite. See the note there for why
    `override_settings` cannot do this.
    """
    return patch.dict(SimpleRateThrottle.THROTTLE_RATES, rates)


class CustomerThrottleTests(APITestBase):
    """The regression guard for the hole customer accounts opened."""

    @with_throttle_rates(customer="2/min")
    def test_a_signed_in_customer_is_metered_on_a_public_endpoint(self):
        """Signing in must not remove the rate limit from the whole API.

        Delete `CustomerRateThrottle` from `DEFAULT_THROTTLE_CLASSES` and this
        is the test that fails: `AnonRateThrottle` steps aside for an
        authenticated request and `StaffRateThrottle` does not recognise a
        `Customer`, so without it the third call — and the ten-thousandth —
        both return 200.
        """
        self.as_customer()

        for _ in range(2):
            self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)

        self.assertEqual(self.client.get(PUBLIC_URL).status_code, 429)

    @with_throttle_rates(customer="2/min")
    def test_the_customer_budget_is_per_account_not_per_address(self):
        """Two customers on one connection do not share a bucket.

        A household behind one router is the ordinary case, not an attack.
        """
        first = self.make_customer(phone="+919000000201")
        second = self.make_customer(phone="+919000000202")

        self.as_customer(first)
        for _ in range(2):
            self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)
        self.assertEqual(self.client.get(PUBLIC_URL).status_code, 429)

        self.as_customer(second)
        self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)

    @with_throttle_rates(customer="240/min", staff="2/min")
    def test_a_customer_does_not_spend_the_staff_budget(self):
        """The two populations are metered separately, in both directions."""
        self.as_customer()
        for _ in range(4):
            self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)

        # The staff budget is untouched by all of that.
        self.as_admin()
        self.assertEqual(self.client.get("/api/products").status_code, 200)


class ThrottleIdentityCollisionTests(APITestBase):
    """That two accounts with the same primary key are two buckets.

    Three models can be `request.user`, each with its own id sequence, so admin
    #1, rider #1 and customer #1 all exist and are three people. Any key built
    from `pk` alone merges them.
    """

    @with_throttle_rates(tracking="2/min")
    def test_an_admin_and_a_customer_with_the_same_pk_do_not_share_a_scope(self):
        """`tracking` is public, so both of them can reach it.

        This is the collision DRF's own `ScopedRateThrottle` has — it keys on a
        bare `request.user.pk` — and the reason this project substitutes
        `NamespacedScopedRateThrottle` for it.
        """
        customer = self.make_customer()
        # The collision is *forced* rather than hoped for. Postgres keeps its
        # sequences across a rolled-back test, so the two tables' ids drift
        # apart as the suite runs and a test that merely created one of each
        # would stop reproducing the bug without anyone noticing.
        admin = AdminUser.objects.create(
            pk=customer.pk,
            email="collide@edawr.test",
            password_hash=hash_password(ADMIN_PASSWORD),
            role=AdminUser.ADMIN,
        )
        self.assertEqual(admin.pk, customer.pk)

        product = self.make_product(stock=5)
        order = self.place_order(product, 1)
        url = f"/api/store/orders/{order.tracking_token}"

        self.as_customer(customer)
        for _ in range(2):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 429)

        # Same scope, same primary key, different table: a fresh budget.
        self.as_admin(admin)
        self.assertEqual(self.client.get(url).status_code, 200)


class AnonymousThrottleTests(APITestBase):
    """That adding two throttle classes did not stop anonymous metering."""

    @with_throttle_rates(anon="2/min")
    def test_an_anonymous_caller_is_still_metered(self):
        self.as_anonymous()

        for _ in range(2):
            self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)

        self.assertEqual(self.client.get(PUBLIC_URL).status_code, 429)

    @with_throttle_rates(anon="2/min", customer="240/min")
    def test_signing_in_does_not_inherit_the_anonymous_budget(self):
        """The two are separate buckets, so an exhausted IP can still sign in.

        This is the shape of the bug in reverse: a customer whose neighbours
        have spent the anonymous budget must still be able to use their own.
        """
        self.as_anonymous()
        for _ in range(2):
            self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)
        self.assertEqual(self.client.get(PUBLIC_URL).status_code, 429)

        self.as_customer()
        self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)


class CustomerAuthScopeTests(APITestBase):
    """That customer sign-in has its own budget, not a share of `login`.

    Both scopes key on the IP address, because neither request carries a token
    yet. On a carrier NAT — which is most of Aizawl on mobile data — one shared
    bucket means a shopper mistyping their password can stop a rider signing in
    to start a shift.
    """

    @with_throttle_rates(customer_auth="2/min", login="10/min")
    def test_exhausting_customer_sign_in_leaves_staff_sign_in_working(self):
        self.as_anonymous()
        admin = self.make_admin()
        wrong = {"phone": "+919000000301", "password": "not-the-password"}

        for _ in range(2):
            response = self.client.post("/api/auth/customer/login", wrong, format="json")
            self.assertEqual(response.status_code, 401)

        response = self.client.post("/api/auth/customer/login", wrong, format="json")
        self.assertEqual(response.status_code, 429)

        # The rider's and the manager's way in is untouched by all of that.
        response = self.client.post(
            "/api/auth/login",
            {"email": admin.email, "password": ADMIN_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200)


class LocationScopeTests(APITestBase):
    """That position reporting has its own budget, not a share of `staff`.

    Location is the highest-frequency authenticated call in this API — a fix
    every few seconds for the length of a delivery, against endpoints a rider
    otherwise touches four times an order. Without its own scope it is metered
    only by `staff`, and a reporting loop wedged on a retry would spend the
    whole 600/min allowance on telemetry.

    What that costs is the point: the next thing the rider could not do is
    accept an order. The work must not be starvable by the instrumentation
    watching it.
    """

    POSITION = {"latitude": 23.7640, "longitude": 92.7178}

    @with_throttle_rates(rider_location="2/min", staff="600/min")
    def test_a_flood_of_positions_does_not_lock_a_rider_out_of_working(self):
        rider = self.make_rider()
        self.as_rider(rider)

        for _ in range(2):
            response = self.client.post(
                "/api/delivery/location", self.POSITION, format="json"
            )
            self.assertEqual(response.status_code, 200, response.data)

        # The third fix is refused...
        response = self.client.post(
            "/api/delivery/location", self.POSITION, format="json"
        )
        self.assertEqual(response.status_code, 429)

        # ...and the rider can still do their job.
        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")
        self.assertEqual(response.status_code, 200, response.data)

    @with_throttle_rates(customer_location="2/min")
    def test_the_customers_own_position_is_metered(self):
        """Public and unauthenticated — the tracking token is the credential."""
        product = self.make_product(stock=10)
        order = self.place_order(product)
        url = f"/api/store/orders/{order.tracking_token}/location"
        self.as_anonymous()

        for _ in range(2):
            response = self.client.post(url, self.POSITION, format="json")
            self.assertEqual(response.status_code, 204)

        response = self.client.post(url, self.POSITION, format="json")
        self.assertEqual(response.status_code, 429)
