"""A signed-in customer's order history, and what verification gates.

The assertion this file exists for is
`test_an_unverified_customer_cannot_see_a_guest_order_with_their_number`.
Everything else here is ordinary list-endpoint behaviour; that one is the
privacy rule, and it is the difference between an account and a way to read a
stranger's address.
"""

from django.utils import timezone

from api.models import Customer, Order
from api.tests.base import CUSTOMER_PHONE, APITestBase

URL = "/api/customer/orders"
CLAIM = "/api/customer/orders/claim"


class OrderHistoryVisibilityTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="62.00", stock=50)

    def _guest_order_from(self, phone: str) -> Order:
        """An order placed with no account, carrying `phone` as the contact."""
        return self.place_order(self.product, 1, customer_phone=phone)

    def test_an_unverified_customer_cannot_see_a_guest_order_with_their_number(self):
        """**The privacy rule.**

        Setting a password proves you know a number, not that you hold the SIM.
        Without this, typing a stranger's number into the sign-up form hands
        over their name, their delivery address and everything they ordered.
        """
        order = self._guest_order_from(CUSTOMER_PHONE)
        customer = self.make_customer(phone=CUSTOMER_PHONE, verified=False)
        self.as_customer(customer)

        response = self.client.get(URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
        self.assertIsNone(order.customer_id)

    def test_a_verified_customer_sees_guest_orders_with_their_number(self):
        """The same rows, once the number is proved. Nothing else changes."""
        self._guest_order_from(CUSTOMER_PHONE)
        customer = self.make_customer(phone=CUSTOMER_PHONE, verified=True)
        self.as_customer(customer)

        response = self.client.get(URL)

        self.assertEqual(len(response.data), 1)

    def test_an_order_placed_while_signed_in_is_visible_without_verification(self):
        """Rule 1 carries the whole feature today, since nothing verifies yet."""
        customer = self.as_customer()
        self.client.post(
            "/api/store/orders", self.checkout_payload(self.product, 1), format="json"
        )

        # `place_order` in the fixture resets credentials; sign back in.
        self.as_customer(customer)
        response = self.client.get(URL)

        self.assertEqual(len(response.data), 1)

    def test_a_verified_customer_never_sees_an_order_owned_by_someone_else(self):
        """Indian mobile numbers get recycled after disconnection.

        Without the `customer__isnull=True` guard, a new registrant of a
        reassigned number would verify it and inherit the previous owner's
        history.
        """
        order = self._guest_order_from(CUSTOMER_PHONE)
        previous_owner = self.make_customer(phone="+919000000601")
        Order.objects.filter(pk=order.pk).update(customer=previous_owner)

        new_registrant = self.make_customer(phone=CUSTOMER_PHONE, verified=True)
        self.as_customer(new_registrant)

        response = self.client.get(URL)

        self.assertEqual(response.data, [])

    def test_another_customers_orders_are_never_visible(self):
        other = self.make_customer(phone="+919000000602")
        order = self._guest_order_from("+919000000602")
        Order.objects.filter(pk=order.pk).update(customer=other)

        self.as_customer(self.make_customer(phone=CUSTOMER_PHONE, verified=True))
        response = self.client.get(URL)

        self.assertEqual(response.data, [])


class OrderHistoryShapeTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="62.00", stock=50)
        self.customer = self.make_customer()
        for _ in range(3):
            order = self.place_order(self.product, 1)
            Order.objects.filter(pk=order.pk).update(customer=self.customer)
        self.as_customer(self.customer)

    def test_newest_first(self):
        response = self.client.get(URL)

        ids = [row["id"] for row in response.data]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_the_total_count_header_is_the_whole_set_not_the_page(self):
        response = self.client.get(f"{URL}?limit=2")

        self.assertEqual(len(response.data), 2)
        self.assertEqual(response["X-Total-Count"], "3")

    def test_offset_pages_through(self):
        first = self.client.get(f"{URL}?limit=2")
        second = self.client.get(f"{URL}?limit=2&offset=2")

        self.assertEqual(len(second.data), 1)
        self.assertNotIn(
            second.data[0]["id"], [row["id"] for row in first.data]
        )

    def test_the_body_is_the_customer_facing_projection(self):
        """OrderTrackingSerializer — carries the token, carries no staff data."""
        row = self.client.get(URL).data[0]

        self.assertIn("tracking_token", row)
        self.assertIn("items", row)
        self.assertNotIn("cost_price", row)
        self.assertNotIn("delivery_boy_id", row)

    def test_the_query_count_does_not_grow_with_the_number_of_orders(self):
        """Guards against the nested serializer regressing into N+1.

        Four queries, and none of them per-order: resolving the token to a
        `Customer` row (which is what makes deactivation immediate), the count
        for `X-Total-Count`, the page itself, and one prefetch for the items of
        every order on it. Three orders here, so an N+1 regression would show
        as six or more.
        """
        with self.assertNumQueries(4):
            list(self.client.get(URL).data)

    def test_an_anonymous_caller_is_refused(self):
        self.as_anonymous()

        self.assertEqual(self.client.get(URL).status_code, 401)


class OrderClaimTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="62.00", stock=50)

    def test_claiming_an_unowned_order_links_it(self):
        order = self.place_order(self.product, 1)
        customer = self.as_customer()

        response = self.client.post(
            CLAIM, {"tracking_token": order.tracking_token}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.customer_id, customer.pk)

    def test_the_claimed_order_then_appears_in_the_history(self):
        """Even unverified — the token is the evidence, not the number."""
        order = self.place_order(self.product, 1, customer_phone="+919000000701")
        self.as_customer()

        self.client.post(CLAIM, {"tracking_token": order.tracking_token}, format="json")

        self.assertEqual(len(self.client.get(URL).data), 1)

    def test_claiming_twice_is_a_success_not_a_conflict(self):
        order = self.place_order(self.product, 1)
        self.as_customer()
        body = {"tracking_token": order.tracking_token}

        self.assertEqual(self.client.post(CLAIM, body, format="json").status_code, 200)
        self.assertEqual(self.client.post(CLAIM, body, format="json").status_code, 200)

    def test_an_order_owned_by_someone_else_cannot_be_claimed(self):
        order = self.place_order(self.product, 1)
        owner = self.make_customer(phone="+919000000702")
        Order.objects.filter(pk=order.pk).update(customer=owner)

        self.as_customer()
        response = self.client.post(
            CLAIM, {"tracking_token": order.tracking_token}, format="json"
        )

        self.assertEqual(response.status_code, 404)
        order.refresh_from_db()
        self.assertEqual(order.customer_id, owner.pk)

    def test_an_unknown_token_is_a_404(self):
        self.as_customer()

        response = self.client.post(
            CLAIM, {"tracking_token": "not-a-real-token"}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    def test_an_anonymous_caller_cannot_claim(self):
        order = self.place_order(self.product, 1)
        self.as_anonymous()

        response = self.client.post(
            CLAIM, {"tracking_token": order.tracking_token}, format="json"
        )

        self.assertEqual(response.status_code, 401)
        order.refresh_from_db()
        self.assertIsNone(order.customer_id)
