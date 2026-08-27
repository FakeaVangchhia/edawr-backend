"""Opening hours, the pause switch, the delivery zone, and failed deliveries.

Four features that did not exist, grouped because they are one story: what
happens when the store *cannot* fulfil an order, at each of the four points it
can find that out — before the shop opens, while a manager has it paused, when
the address is out of range, and at the customer's door.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from api.models import AuditLog, Order, StoreSettings
from api.tests.base import APITestBase


def _shift(base: time, hours: int) -> time:
    """`base` moved by whole hours, wrapping at midnight."""
    moved = datetime.combine(datetime.today(), base) + timedelta(hours=hours)
    return moved.time().replace(second=0, microsecond=0)


class ClosedStoreTests(APITestBase):
    """A 3am order used to be accepted and promised in fifteen minutes."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)

    def _checkout(self):
        self.as_anonymous()
        return self.client.post(
            "/api/store/orders", self.checkout_payload(self.product, 1), format="json"
        )

    def test_paused_store_refuses_checkout(self):
        self.open_store(is_accepting_orders=False)

        response = self._checkout()

        # 503, not 409: checkout already answers 409 for "these items are gone",
        # and the storefront reads that to grey out cart rows.
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Order.objects.count(), 0)

    def test_the_pause_message_reaches_the_customer(self):
        self.open_store(
            is_accepting_orders=False, closed_message="Power cut - back by 4pm."
        )

        self.assertEqual(self._checkout().data["detail"], "Power cut - back by 4pm.")

    def test_a_paused_store_says_so_without_a_message(self):
        self.open_store(is_accepting_orders=False, closed_message="")

        self.assertIn("paused", self._checkout().data["detail"].lower())

    def test_outside_opening_hours_refuses_checkout(self):
        """A window that excludes now, expressed without patching the clock."""
        now = timezone.now().astimezone(self.store_timezone()).time()
        self.open_store(opens_at=_shift(now, 2), closes_at=_shift(now, 4))

        response = self._checkout()

        self.assertEqual(response.status_code, 503)
        self.assertIn("closed", response.data["detail"].lower())

    def test_a_window_that_crosses_midnight_is_open_inside_it(self):
        now = timezone.now().astimezone(self.store_timezone()).time()
        # Opens an hour ago, closes an hour from now. Whenever the suite runs
        # near midnight that pair straddles it, which is the case a naive
        # `opens <= now < closes` gets wrong.
        self.open_store(opens_at=_shift(now, -1), closes_at=_shift(now, 1))

        self.assertEqual(self._checkout().status_code, 201)

    def test_equal_open_and_close_means_always_open(self):
        self.open_store(opens_at=time(9, 0), closes_at=time(9, 0))

        self.assertEqual(self._checkout().status_code, 201)

    def test_store_config_tells_the_storefront_before_the_address_form(self):
        self.open_store(is_accepting_orders=False, closed_message="Stock-take.")
        self.as_anonymous()

        config = self.client.get("/api/store/config").data

        self.assertFalse(config["is_open"])
        # The same sentence checkout would refuse with, from the same method.
        self.assertEqual(config["closed_reason"], "Stock-take.")

    def test_an_open_store_reports_no_reason(self):
        self.as_anonymous()

        config = self.client.get("/api/store/config").data

        self.assertTrue(config["is_open"])
        self.assertEqual(config["closed_reason"], "")


class SettingsFallbackTests(APITestBase):
    """`StoreSettings.load()` must survive a database with no settings row.

    Migration 0007 creates it, so nothing in the normal course of events reaches
    the fallback — which is why a `TypeError` lived there unnoticed. It is the
    path taken by a restored dump, a faked migration, or a row somebody deleted,
    and the whole reason it exists is that a store with no opening hours should
    be created rather than crashed on.
    """

    def test_the_row_is_recreated_rather_than_crashed_on(self):
        StoreSettings.objects.all().delete()

        row = StoreSettings.load()

        self.assertEqual(row.pk, 1)
        # `time`, not `str`. Django does not run `to_python` on a default, so a
        # string default survives into an instance built in memory and every
        # comparison below raises.
        self.assertIsInstance(row.opens_at, time)
        self.assertIsInstance(row.closes_at, time)

    def test_a_recreated_row_can_answer_whether_the_store_is_open(self):
        StoreSettings.objects.all().delete()

        row = StoreSettings.load()

        # These three raised TypeError when the defaults were strings, turning
        # the fallback into a 500 on every config call and every checkout.
        self.assertIsInstance(row.within_hours(), bool)
        self.assertIsInstance(row.is_open(), bool)
        self.assertIsInstance(row.closed_reason(), str)

    def test_the_storefront_still_gets_a_config_with_no_row(self):
        StoreSettings.objects.all().delete()
        self.as_anonymous()

        response = self.client.get("/api/store/config")

        self.assertEqual(response.status_code, 200)
        self.assertIn("is_open", response.data)


class DeliveryZoneTests(APITestBase):
    """An address in Mumbai used to be charged and promised in 15 minutes."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)

    def _checkout(self, **coords):
        self.as_anonymous()
        return self.client.post(
            "/api/store/orders",
            self.checkout_payload(self.product, 1, **coords),
            format="json",
        )

    def test_an_address_outside_the_radius_is_refused(self):
        response = self._checkout(customer_latitude=19.0760, customer_longitude=72.8777)

        self.assertEqual(response.status_code, 400)
        self.assertIn("delivery area", response.data["detail"])
        self.assertEqual(Order.objects.count(), 0)

    def test_the_refusal_names_the_distance_and_the_limit(self):
        detail = self._checkout(
            customer_latitude=19.0760, customer_longitude=72.8777
        ).data["detail"]

        self.assertIn("km from the store", detail)
        self.assertIn("8 km", detail)

    def test_an_address_inside_the_radius_is_accepted(self):
        self.assertEqual(
            self._checkout(
                customer_latitude=23.7300, customer_longitude=92.7200
            ).status_code,
            201,
        )

    def test_widening_the_radius_admits_the_same_address(self):
        far = {"customer_latitude": 23.8000, "customer_longitude": 92.8000}
        self.assertEqual(self._checkout(**far).status_code, 400)

        self.open_store(delivery_radius_km=50.0)

        self.assertEqual(self._checkout(**far).status_code, 201)

    def test_an_order_with_no_position_is_still_accepted(self):
        """Geolocation is opt-in; declining it is not grounds to turn someone away."""
        response = self._checkout(customer_latitude=None)

        self.assertEqual(response.status_code, 201)
        order = Order.objects.get()
        # NULL, not the store's coordinates. That distinction is the whole point
        # - it used to record the customer as standing at the counter.
        self.assertIsNone(order.customer_latitude)
        self.assertIsNone(order.customer_longitude)

    def test_half_a_position_is_a_client_bug_not_an_unknown(self):
        self.as_anonymous()
        payload = self.checkout_payload(self.product, 1)
        payload.pop("customer_longitude")

        response = self.client.post("/api/store/orders", payload, format="json")

        self.assertEqual(response.status_code, 400)

    def test_an_unpositioned_order_reaches_riders_at_unknown_distance(self):
        rider = self.make_rider()
        order = self.place_order(self.product, 1, customer_latitude=None)
        self.advance(order, Order.PACKING, Order.READY)
        self.as_rider(rider)

        feed = self.client.get(f"/api/delivery/{rider.id}/dashboard").data
        rows = feed["incoming_orders"] or (
            [feed["active_order"]] if feed["active_order"] else []
        )

        # Present, and honest about not knowing - never a confident 0.0 km.
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["offered_distance_km"])


class FailedDeliveryTests(APITestBase):
    """`Dispatched` used to lead only to `Delivered`."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 3)
        self.rider = self.make_rider()
        self.advance(self.order, Order.PACKING, Order.READY, Order.DISPATCHED)
        self.order.delivery_boy = self.rider
        self.order.save(update_fields=["delivery_boy"])

    def _fail(self, reason="Customer refused the order at the door"):
        return self.client.patch(
            f"/api/orders/{self.order.id}/status",
            {"status": Order.FAILED, "reason": reason},
            format="json",
        )

    def test_a_rider_can_report_a_failed_delivery(self):
        self.as_rider(self.rider)

        response = self._fail()

        self.assertEqual(response.status_code, 200, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.FAILED)
        self.assertEqual(
            self.order.cancellation_reason, "Customer refused the order at the door"
        )

    def test_a_failure_must_carry_a_reason(self):
        self.as_rider(self.rider)

        self.assertEqual(self._fail(reason="").status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.DISPATCHED)

    def test_failing_does_not_restock_on_its_own(self):
        """The bag is on a bike. Stock must not reappear until it is back."""
        self.as_rider(self.rider)

        self._fail()

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

    def test_a_manager_returns_the_stock_when_the_goods_come_back(self):
        self.as_rider(self.rider)
        self._fail()
        self.as_admin()

        response = self.client.post(f"/api/orders/{self.order.id}/restock")

        self.assertEqual(response.status_code, 200, response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_restocking_twice_does_not_invent_inventory(self):
        self.as_rider(self.rider)
        self._fail()
        self.as_admin()
        self.client.post(f"/api/orders/{self.order.id}/restock")

        second = self.client.post(f"/api/orders/{self.order.id}/restock")

        self.assertEqual(second.status_code, 409)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_only_a_failed_order_can_be_restocked(self):
        self.as_admin()

        response = self.client.post(f"/api/orders/{self.order.id}/restock")

        self.assertEqual(response.status_code, 409)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

    def test_a_failed_order_is_terminal(self):
        self.as_rider(self.rider)
        self._fail()

        response = self.client.patch(
            f"/api/orders/{self.order.id}/status",
            {"status": Order.DELIVERED},
            format="json",
        )

        self.assertEqual(response.status_code, 409)

    def test_a_failed_order_leaves_the_live_board(self):
        self.as_rider(self.rider)
        self._fail()
        self.as_admin()

        rows = self.client.get("/api/orders?open=true").data

        self.assertEqual(list(rows), [])

    def test_the_console_can_tell_the_stock_is_still_out(self):
        """`restocked_at` drives the console's "Return stock to shelf" button.

        Without the field on the wire it reads as `undefined` in TypeScript, the
        `=== null` test is false, and the button never appears — so the only
        control in the system that returns a failed delivery's stock is
        unreachable, on an endpoint that works perfectly.
        """
        self.as_rider(self.rider)
        self._fail()
        self.as_admin()

        row = self.client.get(f"/api/orders?limit=1&q={self.order.id}").data[0]
        self.assertIsNone(row["restocked_at"])

        self.client.post(f"/api/orders/{self.order.id}/restock")

        row = self.client.get(f"/api/orders?limit=1&q={self.order.id}").data[0]
        self.assertIsNotNone(row["restocked_at"])

    def test_the_customer_is_not_told_about_the_shelf(self):
        """Tracking is the narrow shape. Inventory state is not the customer's."""
        self.as_rider(self.rider)
        self._fail()
        self.as_anonymous()

        tracked = self.client.get(
            f"/api/store/orders/{self.order.tracking_token}"
        ).data

        self.assertNotIn("restocked_at", tracked)
        self.assertEqual(tracked["status_label"], "Delivery failed")

    def test_a_delivered_order_cannot_be_failed(self):
        self.advance(self.order, Order.DELIVERED)
        self.as_rider(self.rider)

        self.assertEqual(self._fail().status_code, 409)


class StoreSettingsEndpointTests(APITestBase):
    def test_a_manager_can_pause_the_store(self):
        self.as_manager()

        response = self.client.patch(
            "/api/settings",
            {"is_accepting_orders": False, "closed_message": "Back in 20."},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(StoreSettings.load().is_accepting_orders)

    def test_pausing_is_audited_in_words(self):
        self.as_manager()

        self.client.patch(
            "/api/settings", {"is_accepting_orders": False}, format="json"
        )

        entry = AuditLog.objects.get(entity="settings")
        self.assertEqual(entry.summary, "Paused new orders")

    def test_a_partial_write_leaves_the_pause_switch_alone(self):
        """PATCH, and no PUT: a client editing hours must not reopen a shut shop."""
        self.open_store(is_accepting_orders=False)
        self.as_manager()

        self.client.patch("/api/settings", {"opens_at": "06:00"}, format="json")

        row = StoreSettings.load()
        self.assertFalse(row.is_accepting_orders)
        self.assertEqual(row.opens_at, time(6, 0))

    def test_a_slipped_decimal_point_in_the_radius_is_refused(self):
        self.as_manager()

        response = self.client.patch(
            "/api/settings", {"delivery_radius_km": 800}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_settings_require_a_console_token(self):
        self.as_anonymous()

        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_a_rider_token_is_forbidden_not_unauthenticated(self):
        self.as_rider(self.make_rider())

        self.assertEqual(self.client.get("/api/settings").status_code, 403)
