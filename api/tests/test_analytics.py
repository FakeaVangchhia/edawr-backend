"""The dashboard's arithmetic, checked against orders whose totals are known.

Money is asserted with `assertMoney`, never against a float — the same rule the
rest of this suite follows, and the reason `COERCE_DECIMAL_TO_STRING` is off.

The two properties worth stating outright, because both are decisions rather
than consequences:

- **Cancelled orders are excluded from revenue everywhere.** They are counted
  only by the cancellation rate. One rule, applied identically, so two screens
  cannot disagree about what a week contained.
- **`was_late` is a stamp, not a derivation.** It is written once at delivery, so
  editing a delivery tier afterwards does not silently rewrite last month's
  on-time rate.
"""

from datetime import date, timedelta

from django.utils import timezone

from api.models import Order
from api.tests.base import APITestBase


class AnalyticsAccessTests(APITestBase):
    def test_analytics_needs_a_console_token(self):
        self.as_anonymous()
        self.assertEqual(self.client.get("/api/analytics/summary").status_code, 401)

    def test_rider_cannot_read_analytics(self):
        """A rider's token is valid and is not a console token. 403, not 401."""
        self.as_rider()
        self.assertEqual(self.client.get("/api/analytics/summary").status_code, 403)


class SummaryTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="50.00", stock=100)

    def test_empty_store_reports_zeroes_not_nulls(self):
        """A new store has no sales. Every tile must still render a number —
        `Sum` over no rows is NULL, which would draw an empty dashboard."""
        self.as_admin()
        data = self.client.get("/api/analytics/summary").data
        self.assertMoney(data["revenue"]["value"], "0.00")
        self.assertMoney(data["orders"]["value"], "0")
        self.assertMoney(data["average_order_value"]["value"], "0.00")

    def test_revenue_sums_grand_total(self):
        first = self.place_order(self.product, quantity=2)
        second = self.place_order(self.product, quantity=1)
        expected = first.grand_total + second.grand_total

        self.as_admin()
        data = self.client.get("/api/analytics/summary").data
        self.assertMoney(data["revenue"]["value"], str(expected))
        self.assertMoney(data["orders"]["value"], "2")

    def test_cancelled_orders_leave_revenue(self):
        kept = self.place_order(self.product, quantity=2)
        doomed = self.place_order(self.product, quantity=2)

        self.as_admin()
        response = self.client.patch(
            f"/api/orders/{doomed.pk}/status",
            {"status": Order.CANCELLED, "reason": "Customer changed their mind"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        data = self.client.get("/api/analytics/summary").data
        self.assertMoney(data["revenue"]["value"], str(kept.grand_total))
        self.assertMoney(data["orders"]["value"], "1")
        # One of two orders cancelled.
        self.assertMoney(data["cancellation_rate"]["value"], "50.00")

    def test_average_order_value(self):
        first = self.place_order(self.product, quantity=2)
        second = self.place_order(self.product, quantity=4)
        total = first.grand_total + second.grand_total

        self.as_admin()
        data = self.client.get("/api/analytics/summary").data
        self.assertMoney(data["average_order_value"]["value"], str(total / 2))

    def test_summary_carries_a_previous_period(self):
        self.as_admin()
        data = self.client.get("/api/analytics/summary?from=2026-01-10&to=2026-01-19").data
        self.assertIn("previous", data["revenue"])
        self.assertMoney(data["revenue"]["previous"], "0.00")


class OnTimeTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=100)

    def deliver(self, order: Order, *, minutes: int) -> Order:
        """Deliver an order that was placed `minutes` ago.

        `created_at` is moved backwards rather than the clock forwards, because
        `advance_status` stamps from `timezone.now()` and the lateness it records
        is the gap between the two.
        """
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )
        order.refresh_from_db()
        return self.advance(order, Order.PACKING, Order.READY,
                            Order.DISPATCHED, Order.DELIVERED)

    def test_on_time_and_late_are_stamped_at_delivery(self):
        quick = self.deliver(self.place_order(self.product), minutes=8)
        slow = self.deliver(self.place_order(self.product), minutes=40)

        self.assertFalse(quick.was_late)
        self.assertEqual(quick.delivered_in_minutes, 8)
        self.assertTrue(slow.was_late)
        self.assertEqual(slow.delivered_in_minutes, 40)

    def test_delivery_endpoint_reports_the_rate(self):
        self.deliver(self.place_order(self.product), minutes=5)
        self.deliver(self.place_order(self.product), minutes=9)
        self.deliver(self.place_order(self.product), minutes=30)

        self.as_admin()
        data = self.client.get("/api/analytics/delivery").data
        self.assertEqual(data["delivered"], 3)
        self.assertEqual(data["late"], 1)
        self.assertAlmostEqual(data["on_time_rate"], 66.7, places=1)

    def test_editing_the_promise_does_not_rewrite_history(self):
        """The reason `was_late` is a column and not a computed property."""
        order = self.deliver(self.place_order(self.product), minutes=10)
        self.assertFalse(order.was_late)

        # The store later shortens its promise. The delivered order was on time
        # against the promise it was actually sold under, and stays that way.
        Order.objects.filter(pk=order.pk).update(promised_minutes=5)

        self.as_admin()
        data = self.client.get("/api/analytics/delivery").data
        self.assertEqual(data["late"], 0)

    def test_undelivered_orders_are_not_counted_as_on_time(self):
        self.place_order(self.product)
        self.as_admin()
        data = self.client.get("/api/analytics/delivery").data
        self.assertEqual(data["delivered"], 0)
        self.assertIsNone(data["average_minutes"])


class SeriesTests(APITestBase):
    def test_revenue_series_fills_empty_days(self):
        """A chart that omits quiet days draws a straight line across them and
        reports a dead week as steady trade."""
        self.as_admin()
        data = self.client.get("/api/analytics/revenue?from=2026-03-01&to=2026-03-07").data
        self.assertEqual(len(data), 7)
        # `response.data` is pre-render, so this is a date object, not a string.
        self.assertEqual(data[0]["date"], date(2026, 3, 1))
        self.assertMoney(data[0]["revenue"], "0.00")

    def test_reversed_range_is_swapped_not_rejected(self):
        self.as_admin()
        data = self.client.get("/api/analytics/revenue?from=2026-03-07&to=2026-03-01").data
        self.assertEqual(len(data), 7)

    def test_garbage_dates_fall_back_to_the_default_window(self):
        self.as_admin()
        response = self.client.get("/api/analytics/revenue?from=not-a-date&to=")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 30)


class TopProductTests(APITestBase):
    def test_products_rank_by_units_sold(self):
        popular = self.make_product(name="Parle-G", price="10.00", stock=100)
        quiet = self.make_product(name="Imported Olives", price="400.00", stock=100)
        self.place_order(popular, quantity=8)
        self.place_order(quiet, quantity=1)

        self.as_admin()
        data = self.client.get("/api/analytics/products").data
        self.assertEqual(data[0]["name"], "Parle-G")
        self.assertEqual(data[0]["units"], 8)

    def test_bottom_direction_reverses_the_order(self):
        popular = self.make_product(name="Parle-G", price="10.00", stock=100)
        quiet = self.make_product(name="Imported Olives", price="400.00", stock=100)
        self.place_order(popular, quantity=8)
        self.place_order(quiet, quantity=1)

        self.as_admin()
        data = self.client.get("/api/analytics/products?direction=bottom").data
        self.assertEqual(data[0]["name"], "Imported Olives")

    def test_category_share(self):
        self.place_order(self.make_product(category="Dairy & Bread", stock=50), quantity=2)
        self.as_admin()
        data = self.client.get("/api/analytics/categories").data
        self.assertEqual(data[0]["category"], "Dairy & Bread")
        self.assertEqual(data[0]["units"], 2)


class InventoryTests(APITestBase):
    def test_counts_out_of_stock_and_low_stock_separately(self):
        """Out of stock and low stock are different problems: one is a sale you
        are already losing, the other is one you are about to."""
        self.make_product(name="Sold Out", stock=0)
        low = self.make_product(name="Nearly Out", stock=3)
        low.reorder_level = 5
        low.save(update_fields=["reorder_level"])
        self.make_product(name="Plenty", stock=90)

        self.as_admin()
        data = self.client.get("/api/analytics/inventory").data
        self.assertEqual(data["total_products"], 3)
        self.assertEqual(data["out_of_stock"], 1)
        self.assertEqual(data["low_stock"], 1)

    def test_stock_is_valued_at_cost(self):
        """Valuing the shelf at retail would book margin nobody has earned."""
        self.make_product(name="One", stock=10)  # cost_price is 40.00 in the fixture
        self.as_admin()
        data = self.client.get("/api/analytics/inventory").data
        self.assertMoney(data["stock_value"], "400.00")
        self.assertEqual(data["stock_units"], 10)

    def test_reorder_list_puts_the_emptiest_first(self):
        self.make_product(name="Sold Out", stock=0)
        self.make_product(name="Plenty", stock=90)
        self.as_admin()
        data = self.client.get("/api/analytics/inventory").data
        self.assertEqual(data["items"][0]["name"], "Sold Out")
