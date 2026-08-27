"""Checkout: the one public endpoint that writes rows and moves stock."""

from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from rest_framework.throttling import SimpleRateThrottle

from api.checkout import place_order
from api.models import Customer, Order, OrderItem, Product
from api.tests.base import APITestBase

URL = "/api/store/orders"


def with_throttle_rates(**rates):
    """Temporarily change individual throttle scopes.

    `override_settings(REST_FRAMEWORK=...)` does *not* work for this, which is
    worth knowing before you try it. DRF reloads `api_settings` on Django's
    `setting_changed` signal, but `SimpleRateThrottle.THROTTLE_RATES` is a class
    attribute bound to the original dict object at import time, and reloading
    the settings object never rebinds it. Patching the dict the throttle
    actually reads is the only thing that takes effect.
    """
    return patch.dict(SimpleRateThrottle.THROTTLE_RATES, rates)


class CheckoutTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="62.00", mrp="66.00", stock=10)
        self.as_anonymous()

    # --- the happy path --------------------------------------------------
    def test_places_an_order_without_any_credentials(self):
        response = self.client.post(URL, self.checkout_payload(self.product, 2), format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], Order.PLACED)
        self.assertEqual(len(response.data["items"]), 1)
        self.assertTrue(response.data["tracking_token"])

    def test_totals_are_computed_from_the_catalogue(self):
        response = self.client.post(URL, self.checkout_payload(self.product, 3), format="json")

        self.assertMoney(response.data["items_total"], "186.00")
        self.assertMoney(response.data["delivery_fee"], "15.00")
        self.assertMoney(response.data["handling_fee"], "5.00")
        self.assertMoney(response.data["grand_total"], "206.00")

    def test_stock_is_decremented(self):
        self.client.post(URL, self.checkout_payload(self.product, 4), format="json")

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 6)

    def test_line_items_snapshot_name_and_price(self):
        """A later price change must not restate what this customer was charged."""
        self.client.post(URL, self.checkout_payload(self.product, 1), format="json")

        self.product.price = Decimal("99.00")
        self.product.name = "Renamed"
        self.product.save()

        item = OrderItem.objects.get()
        self.assertEqual(item.name, "Amul Taaza Milk")
        self.assertEqual(item.price, Decimal("62.00"))
        self.assertEqual(item.line_total, Decimal("62.00"))

    def test_promise_is_snapshotted_from_the_chosen_tier(self):
        with override_settings(DELIVERY_PROMISE_MINUTES_INSTANT=12):
            response = self.client.post(URL, self.checkout_payload(self.product), format="json")
        self.assertEqual(response.data["promised_minutes"], 12)

        # Re-tuning the tier afterwards must not rewrite it.
        with override_settings(DELIVERY_PROMISE_MINUTES_INSTANT=30):
            order = Order.objects.get(pk=response.data["id"])
            self.assertEqual(order.promised_minutes, 12)

    # --- delivery tiers ---------------------------------------------------
    def test_defaults_to_instant_when_no_tier_is_named(self):
        response = self.client.post(URL, self.checkout_payload(self.product), format="json")

        self.assertEqual(response.data["delivery_type"], Order.INSTANT)
        self.assertEqual(response.data["delivery_type_label"], "Instant")
        self.assertEqual(response.data["promised_minutes"], 15)
        self.assertMoney(response.data["delivery_fee"], "15.00")

    def test_slow_is_cheaper_and_promised_later(self):
        payload = self.checkout_payload(self.product, 2)
        payload["delivery_type"] = Order.SLOW

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["delivery_type"], Order.SLOW)
        self.assertEqual(response.data["promised_minutes"], 45)
        self.assertMoney(response.data["delivery_fee"], "5.00")
        self.assertMoney(response.data["grand_total"], "134.00")

        order = Order.objects.get(pk=response.data["id"])
        self.assertEqual(order.delivery_type, Order.SLOW)
        self.assertEqual(order.promised_minutes, 45)

    def test_a_tier_the_store_does_not_sell_is_rejected(self):
        """A 400 rather than a silent fallback: a client asking for a speed that
        does not exist has a bug, and swallowing it hides the bug behind a
        delivery the customer did not choose."""
        payload = self.checkout_payload(self.product)
        payload["delivery_type"] = "teleport"

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_the_free_delivery_threshold_applies_to_both_tiers(self):
        big = self.make_product(price="250.00", stock=5)

        for tier in (Order.INSTANT, Order.SLOW):
            with self.subTest(delivery_type=tier):
                payload = self.checkout_payload(big, 1)
                payload["delivery_type"] = tier
                response = self.client.post(URL, payload, format="json")

                self.assertEqual(response.status_code, 201)
                self.assertMoney(response.data["delivery_fee"], "0.00")
                self.assertMoney(response.data["grand_total"], "255.00")

    # --- the client cannot name its own price ----------------------------
    def test_client_supplied_totals_are_ignored(self):
        payload = self.checkout_payload(self.product, 2)
        payload.update({
            "items_total": "1.00",
            "grand_total": "1.00",
            "delivery_fee": "0.00",
            "handling_fee": "0.00",
        })

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertMoney(response.data["grand_total"], "144.00")

    def test_client_cannot_choose_its_own_status(self):
        payload = self.checkout_payload(self.product, 2)
        payload["status"] = Order.DELIVERED

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.data["status"], Order.PLACED)

    def test_client_cannot_supply_a_tracking_token(self):
        payload = self.checkout_payload(self.product, 2)
        payload["tracking_token"] = "chosen-by-the-attacker"

        response = self.client.post(URL, payload, format="json")

        self.assertNotEqual(response.data["tracking_token"], "chosen-by-the-attacker")

    # --- stock and availability ------------------------------------------
    def test_rejects_a_basket_larger_than_stock(self):
        response = self.client.post(URL, self.checkout_payload(self.product, 11), format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["unavailable"][0]["available"], 10)
        self.assertEqual(Order.objects.count(), 0)

    def test_unavailable_payload_keeps_product_id_as_a_number(self):
        """The storefront matches these ids against its cart, where they are
        numbers. DRF's ValidationError would have stringified them."""
        response = self.client.post(URL, self.checkout_payload(self.product, 11), format="json")

        self.assertIsInstance(response.data["unavailable"][0]["product_id"], int)

    def test_rejects_an_inactive_product(self):
        self.product.status = Product.INACTIVE
        self.product.save()

        response = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

    def test_rejects_an_unknown_product(self):
        payload = self.checkout_payload(self.product)
        payload["items"] = [{"product_id": 999999, "quantity": 1}]

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 409)

    def test_nothing_is_written_when_one_line_fails(self):
        """The whole order is one transaction: a basket with an impossible line
        must not leave the other line's stock decremented."""
        other = self.make_product(name="Bread", price="45.00", stock=5)
        payload = self.checkout_payload(self.product, 1)
        payload["items"] = [
            {"product_id": self.product.id, "quantity": 1},
            # Above stock but within MAX_QUANTITY_PER_ITEM, so it fails the
            # stock check inside the transaction rather than field validation
            # before it — which is the path this test exists to cover.
            {"product_id": other.id, "quantity": 6},
        ]

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 409)
        self.product.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(other.stock, 5)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)

    def test_duplicate_lines_are_merged_before_the_stock_check(self):
        """Two lines of 6 against stock of 10 must fail as one line of 12,
        not pass twice as 6 <= 10."""
        payload = self.checkout_payload(self.product)
        payload["items"] = [
            {"product_id": self.product.id, "quantity": 6},
            {"product_id": self.product.id, "quantity": 6},
        ]

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

    def test_merged_duplicate_lines_produce_one_item(self):
        payload = self.checkout_payload(self.product)
        payload["items"] = [
            {"product_id": self.product.id, "quantity": 2},
            {"product_id": self.product.id, "quantity": 3},
        ]

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["items"]), 1)
        self.assertEqual(response.data["items"][0]["quantity"], 5)

    # --- validation ------------------------------------------------------
    def test_rejects_an_empty_basket(self):
        payload = self.checkout_payload(self.product)
        payload["items"] = []

        response = self.client.post(URL, payload, format="json")

        self.assertEqual(response.status_code, 400)

    @override_settings(MIN_ORDER_VALUE="500.00")
    def test_enforces_the_minimum_order_value(self):
        response = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Minimum order", response.data["detail"])

    @override_settings(MAX_QUANTITY_PER_ITEM=3)
    def test_enforces_the_per_item_quantity_cap(self):
        response = self.client.post(URL, self.checkout_payload(self.product, 4), format="json")

        self.assertEqual(response.status_code, 400)

    def test_rejects_a_short_address(self):
        response = self.client.post(
            URL, self.checkout_payload(self.product, customer_address="a"), format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_normalises_the_phone_number(self):
        for typed in ["9812345678", "+91 98123 45678", "098123-45678", "919812345678"]:
            with self.subTest(typed=typed):
                response = self.client.post(
                    URL, self.checkout_payload(self.product, 1, customer_phone=typed), format="json"
                )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["customer_phone"], "+919812345678")

    def test_rejects_an_implausible_phone_number(self):
        for typed in ["12345", "1234567890", "abcdefghij", ""]:
            with self.subTest(typed=typed):
                response = self.client.post(
                    URL, self.checkout_payload(self.product, 1, customer_phone=typed), format="json"
                )
                self.assertEqual(response.status_code, 400)

    # --- throttling ------------------------------------------------------
    @with_throttle_rates(checkout="2/hour")
    def test_checkout_is_throttled(self):
        """Without a limit, a loop empties the catalogue's stock into orders."""
        for _ in range(2):
            response = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")
            self.assertEqual(response.status_code, 201)

        response = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")
        self.assertEqual(response.status_code, 429)


class CancellationTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 3)
        self.url = f"/api/store/orders/{self.order.tracking_token}/cancel"

    def test_customer_can_cancel_before_dispatch(self):
        response = self.client.post(self.url, {"reason": "Changed my mind"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.CANCELLED)

    def test_cancelling_restores_stock(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

        self.client.post(self.url, {}, format="json")

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_cancelling_twice_does_not_restore_stock_twice(self):
        """Two taps on a slow connection must not invent inventory."""
        self.client.post(self.url, {}, format="json")
        second = self.client.post(self.url, {}, format="json")

        # 409, not 400: the body was fine, the order had already been cancelled.
        self.assertEqual(second.status_code, 409)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_cannot_cancel_once_a_rider_has_it(self):
        self.advance(self.order, Order.PACKING, Order.READY, Order.DISPATCHED)

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

    def test_unknown_token_is_a_404(self):
        response = self.client.post("/api/store/orders/not-a-real-token/cancel", {}, format="json")
        self.assertEqual(response.status_code, 404)


class PlaceOrderServiceTests(APITestBase):
    """The service function directly, for cases the HTTP layer cannot reach."""

    def test_locks_products_in_primary_key_order(self):
        """Deadlock avoidance: two baskets holding the same products in
        opposite orders must still take the locks in the same sequence."""
        first = self.make_product(name="A", price="60.00", stock=5)
        second = self.make_product(name="B", price="60.00", stock=5)

        order, created = place_order({
            "customer_name": "Test",
            "customer_phone": "+919812345678",
            "customer_address": "Somewhere in Aizawl",
            "items": [
                {"product_id": second.id, "quantity": 1},
                {"product_id": first.id, "quantity": 1},
            ],
        })
        self.assertTrue(created)

        self.assertEqual(
            list(order.items.order_by("product_id").values_list("product_id", flat=True)),
            sorted([first.id, second.id]),
        )


class CheckoutAccountLinkTests(APITestBase):
    """Which orders get a `customer`, and — more importantly — which do not."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="62.00", stock=20)

    def test_a_guest_checkout_still_leaves_the_order_unowned(self):
        """The regression guard for the path that carries the money.

        Guest checkout is the main path here, not a fallback, and every order
        placed before accounts existed is one of these. If this ever fails,
        something has started requiring an account to buy groceries.
        """
        self.as_anonymous()

        response = self.client.post(URL, self.checkout_payload(self.product, 2), format="json")

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(pk=response.data["id"])
        self.assertIsNone(order.customer_id)

    def test_a_signed_in_checkout_links_the_order(self):
        customer = self.as_customer()

        response = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(pk=response.data["id"])
        self.assertEqual(order.customer_id, customer.pk)

    def test_the_typed_contact_details_are_not_overwritten_by_the_account(self):
        """The FK says whose account; the fields say what was typed.

        Ordering for somebody else at their number is the ordinary case, and
        the rider dials what was typed.
        """
        customer = self.as_customer(self.make_customer(name="Account Holder"))
        payload = self.checkout_payload(self.product, 1)
        payload["customer_name"] = "Someone Else"
        payload["customer_phone"] = "+919000000888"

        response = self.client.post(URL, payload, format="json")

        order = Order.objects.get(pk=response.data["id"])
        self.assertEqual(order.customer_id, customer.pk)
        self.assertEqual(order.customer_name, "Someone Else")
        self.assertEqual(order.customer_phone, "+919000000888")

    def test_a_staff_token_does_not_take_ownership_of_an_order(self):
        """Checkout is public, so an admin or rider token reaches it too.

        Neither should end up owning the order — hence `isinstance`, rather
        than a truthiness check on request.user.
        """
        self.as_admin()
        admin_order = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")
        self.assertEqual(admin_order.status_code, 201)
        self.assertIsNone(Order.objects.get(pk=admin_order.data["id"]).customer_id)

        self.as_rider()
        rider_order = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")
        self.assertEqual(rider_order.status_code, 201)
        self.assertIsNone(Order.objects.get(pk=rider_order.data["id"]).customer_id)

    def test_a_replay_does_not_change_who_owns_the_order(self):
        """A replay created nothing, so it changes nothing.

        The one way to reach this is to sign in between a checkout POST and its
        own retry. The honest answer is that the attempt already happened —
        "upgrading" the stored order on a replay would make 200-not-201 a lie.
        """
        self.as_anonymous()
        first = self.client.post(
            URL,
            self.checkout_payload(self.product, 1),
            format="json",
            HTTP_IDEMPOTENCY_KEY="signed-in-halfway-through",
        )
        self.assertEqual(first.status_code, 201)

        self.as_customer()
        replay = self.client.post(
            URL,
            self.checkout_payload(self.product, 1),
            format="json",
            HTTP_IDEMPOTENCY_KEY="signed-in-halfway-through",
        )

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertIsNone(Order.objects.get(pk=first.data["id"]).customer_id)

    def test_a_signed_in_retry_is_still_deduplicated(self):
        """The dedupe read must not gain a `customer` term.

        If it did, one key would create two orders — a guest attempt and a
        signed-in retry would stop matching — which is the double charge the
        key exists to prevent.
        """
        self.as_customer()
        payload = self.checkout_payload(self.product, 1)

        first = self.client.post(
            URL, payload, format="json", HTTP_IDEMPOTENCY_KEY="one-basket-one-order"
        )
        second = self.client.post(
            URL, payload, format="json", HTTP_IDEMPOTENCY_KEY="one-basket-one-order"
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)

    def test_an_expired_customer_token_fails_the_checkout_outright(self):
        """Documented, because it is surprising and the client works around it.

        Authentication runs before permission, so a present-but-invalid token
        is rejected before the view is reached — on a *public* endpoint. The
        storefront's `placeOrder` catches the 401, clears the session and
        retries with no header, reusing the same idempotency key.
        """
        customer = self.make_customer()
        self.as_customer(customer)
        Customer.objects.filter(pk=customer.pk).update(token_version=99)

        response = self.client.post(URL, self.checkout_payload(self.product, 1), format="json")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Order.objects.count(), 0)
