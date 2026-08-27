"""Cash on delivery, recorded rather than assumed.

`payment_method = "cod"` records an intention: this order is to be paid at the
door. Nothing recorded whether it was, so the only way to answer "how much cash
does this rider owe the till?" was to sum `grand_total` over their delivered
orders and trust it — the expected figure used as if it were the actual one,
which is exactly the substitution a cash business cannot make.

Two halves are tested here: the stamp, which must happen on every route to
Delivered, and the reconciliation endpoint, which must pair every figure with
what it was supposed to be.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from api.models import AuditLog, Order
from api.tests.base import APITestBase


class CollectionStampTests(APITestBase):
    """Reaching Delivered records a collection. There is no route that does not."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="60.00", stock=20)
        self.rider = self.make_rider()

    def dispatched(self) -> Order:
        order = self.place_order(self.product, 2)
        order.delivery_boy = self.rider
        order.save(update_fields=["delivery_boy"])
        return self.advance(order, Order.PACKING, Order.READY, Order.DISPATCHED)

    # --- the model -------------------------------------------------------
    def test_advance_status_stamps_the_collection(self):
        """Stamped in `advance_status` rather than in a view, so a future caller
        cannot reach Delivered and forget to record the money."""
        order = self.dispatched()

        changed = order.advance_status(Order.DELIVERED)
        order.save(update_fields=changed)

        self.assertIn("paid_at", changed)
        self.assertIn("amount_collected", changed)
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(order.amount_collected, order.grand_total)

    def test_nothing_is_stamped_short_of_delivery(self):
        order = self.dispatched()

        self.assertIsNone(order.paid_at)
        self.assertIsNone(order.amount_collected)

    def test_a_cancelled_order_records_no_collection(self):
        order = self.place_order(self.product, 2)

        self.as_admin()
        self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.CANCELLED, "reason": "Customer changed their mind"},
            format="json",
        )

        order.refresh_from_db()
        self.assertEqual(order.status, Order.CANCELLED)
        self.assertIsNone(order.paid_at)
        self.assertIsNone(order.amount_collected)

    def test_a_failed_delivery_records_no_collection(self):
        """The bag came back. Recording a collection would be inventing revenue
        for goods the customer refused."""
        order = self.dispatched()

        self.as_rider(self.rider)
        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.FAILED, "reason": "Nobody at the address"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        order.refresh_from_db()
        self.assertIsNone(order.paid_at)
        self.assertIsNone(order.amount_collected)

    # --- the rider's revision --------------------------------------------
    def test_a_rider_delivering_is_recorded_as_the_collector(self):
        order = self.dispatched()

        self.as_rider(self.rider)
        self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED},
            format="json",
        )

        order.refresh_from_db()
        self.assertEqual(order.collected_by_id, self.rider.pk)
        self.assertEqual(order.amount_collected, order.grand_total)

    def test_a_rider_can_record_a_short_payment(self):
        order = self.dispatched()

        self.as_rider(self.rider)
        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED, "amount_collected": "100.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        order.refresh_from_db()
        self.assertMoney(order.amount_collected, "100.00")
        self.assertGreater(order.grand_total, order.amount_collected)

    def test_collecting_nothing_is_a_legitimate_answer(self):
        """"They took the bag and paid nothing" is a real outcome, and one the
        store needs recorded rather than rounded away."""
        order = self.dispatched()

        self.as_rider(self.rider)
        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED, "amount_collected": "0.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        order.refresh_from_db()
        self.assertMoney(order.amount_collected, "0.00")
        self.assertIsNotNone(order.paid_at)

    def test_collecting_more_than_the_total_is_refused(self):
        """Above the total is not a collection — it is change the rider owes
        back, and booking it as revenue makes the till reconcile against money
        the store never kept."""
        order = self.dispatched()

        self.as_rider(self.rider)
        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED, "amount_collected": "99999.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.DISPATCHED)

    def test_a_negative_amount_is_refused(self):
        order = self.dispatched()

        self.as_rider(self.rider)
        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED, "amount_collected": "-1.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_an_amount_on_a_non_delivery_is_refused(self):
        """Silently ignoring it is how a rider comes to believe they recorded a
        collection they did not."""
        order = self.dispatched()

        self.as_rider(self.rider)
        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {
                "status": Order.FAILED,
                "reason": "Refused",
                "amount_collected": "50.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_an_admin_closing_an_order_leaves_the_collector_null(self):
        """`Delivered` is in ADMIN_TARGETS too — a manager closing an order the
        rider could not close is a real situation.

        `collected_by` stays NULL there, and that is the honest record: nobody
        stood at a door and took money on this request. The collection is still
        stamped, because the goods reached the customer and the cash is owed
        either way, and `AuditLog` records which manager clicked.
        """
        order = self.dispatched()

        self.as_admin()
        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        order.refresh_from_db()
        self.assertIsNone(order.collected_by_id)
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(order.amount_collected, order.grand_total)

    def test_an_unattributed_collection_still_reaches_the_till(self):
        """A row with no rider must not be dropped from the reconciliation.
        Money the store is owed does not stop being owed because the report
        cannot name who took it — a missing row is how cash goes missing
        quietly."""
        order = self.dispatched()
        self.as_admin()
        self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED},
            format="json",
        )

        rows = self.client.get("/api/analytics/cash").data["riders"]

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["rider_id"])
        self.assertEqual(rows[0]["orders"], 1)

    def test_a_short_payment_is_written_to_the_audit_log(self):
        order = self.dispatched()

        self.as_rider(self.rider)
        self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED, "amount_collected": "20.00"},
            format="json",
        )

        entry = AuditLog.objects.filter(entity="order", entity_id=order.pk).first()
        self.assertIsNotNone(entry)
        self.assertIn("collected", entry.summary)
        self.assertIn("amount_collected", entry.changes)

    def test_a_full_payment_does_not_clutter_the_audit_summary(self):
        """The log exists to surface the exceptions. Every delivery annotating
        itself with "collected the full amount" buries them."""
        order = self.dispatched()

        self.as_rider(self.rider)
        self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.DELIVERED},
            format="json",
        )

        entry = AuditLog.objects.filter(entity="order", entity_id=order.pk).first()
        self.assertNotIn("collected", entry.summary)


class CashEndpointTests(APITestBase):
    """GET /api/analytics/cash — what should be in the till."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="100.00", stock=100)
        self.rider = self.make_rider()
        self.other = self.make_rider(name="Second", phone="+919000000009")

    def deliver(self, rider, collected: str | None = None) -> Order:
        order = self.place_order(self.product, 1)
        order.delivery_boy = rider
        order.save(update_fields=["delivery_boy"])
        self.advance(order, Order.PACKING, Order.READY, Order.DISPATCHED)

        self.as_rider(rider)
        body = {"status": Order.DELIVERED}
        if collected is not None:
            body["amount_collected"] = collected
        response = self.client.patch(
            f"/api/orders/{order.pk}/status", body, format="json"
        )
        assert response.status_code == 200, response.data
        order.refresh_from_db()
        return order

    def cash(self, **params):
        self.as_admin()
        return self.client.get("/api/analytics/cash", params)

    def test_an_empty_till_is_zeroes_not_nulls(self):
        response = self.cash()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["orders"], 0)
        self.assertMoney(response.data["expected"], "0.00")
        self.assertMoney(response.data["collected"], "0.00")
        self.assertMoney(response.data["shortfall"], "0.00")
        self.assertEqual(response.data["riders"], [])

    def test_it_pairs_expected_against_collected(self):
        full = self.deliver(self.rider)
        short = self.deliver(self.rider, collected="80.00")

        response = self.cash()

        # Derived from the orders rather than hardcoded: `grand_total` is the
        # goods plus a handling fee plus a delivery fee, all of which are
        # environment-configurable, and a test that hardcodes the sum fails the
        # day someone changes a fee — which would be a fee change failing a cash
        # test, and nobody would believe it.
        expected = full.grand_total + short.grand_total
        shortfall = short.grand_total - Decimal("80.00")

        self.assertEqual(response.data["orders"], 2)
        self.assertMoney(response.data["expected"], str(expected))
        self.assertMoney(response.data["collected"], str(expected - shortfall))
        self.assertMoney(response.data["shortfall"], str(shortfall))
        self.assertEqual(response.data["short_orders"], 1)

    def test_it_groups_by_rider(self):
        self.deliver(self.rider, collected="50.00")
        self.deliver(self.other)

        response = self.cash()
        rows = {row["rider_id"]: row for row in response.data["riders"]}

        self.assertEqual(set(rows), {self.rider.pk, self.other.pk})
        self.assertGreater(Decimal(str(rows[self.rider.pk]["shortfall"])), 0)
        self.assertMoney(rows[self.other.pk]["shortfall"], "0.00")
        self.assertEqual(rows[self.other.pk]["short_orders"], 0)
        self.assertEqual(rows[self.rider.pk]["name"], self.rider.name)

    def test_it_groups_by_day(self):
        self.deliver(self.rider)

        response = self.cash()

        self.assertEqual(len(response.data["days"]), 1)
        self.assertEqual(response.data["days"][0]["orders"], 1)

    def test_undelivered_orders_are_absent(self):
        """`paid_at` is the filter, so nothing that has not been paid for can
        appear — including an order still on a bike."""
        self.place_order(self.product, 1)

        self.assertEqual(self.cash().data["orders"], 0)

    def test_it_buckets_by_when_the_cash_arrived(self):
        """The one endpoint in analytics that does not bucket by `created_at`.

        An order placed at 23:50 and delivered at 00:05 is money that reaches
        the till on the second day. Filing it under the first would leave both
        days wrong and the rider arguing with the report.
        """
        order = self.deliver(self.rider)
        Order.objects.filter(pk=order.pk).update(
            paid_at=timezone.now() - timedelta(days=40)
        )

        # Outside the default 30-day window, even though `created_at` is today.
        self.assertEqual(self.cash().data["orders"], 0)

        today = timezone.now().date()
        widened = self.cash(**{"from": str(today - timedelta(days=60)), "to": str(today)})
        self.assertEqual(widened.data["orders"], 1)

    def test_a_manager_may_read_the_till(self):
        """Running the till is a Manager's job, so this is not Admin-only."""
        self.deliver(self.rider)

        self.as_manager()
        self.assertEqual(self.client.get("/api/analytics/cash").status_code, 200)

    def test_it_is_not_public(self):
        self.as_anonymous()
        self.assertEqual(self.client.get("/api/analytics/cash").status_code, 401)

    def test_a_rider_cannot_read_the_till(self):
        self.as_rider(self.rider)
        self.assertEqual(self.client.get("/api/analytics/cash").status_code, 403)
