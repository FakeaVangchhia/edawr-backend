"""Automatic rider assignment.

The rule under test: an order reaching Ready is handed to the nearest eligible
rider in the same transaction, and finding nobody leaves it Ready rather than
failing the manager's request.

Coordinates here are real Aizawl ones. The store and the default customer sit at
(23.7272, 92.7178); a rider given a different base is a rider a measurable
distance away, which is what makes the "nearest wins" and "out of range"
assertions mean something rather than comparing two identical points.
"""

from __future__ import annotations

from django.test import override_settings

from api.models import AuditLog, Order, OrderRejection
from api.tests.base import APITestBase


# ~4 km north of the store, and ~40 km away -- comfortably inside and outside a
# 10 km service radius without sitting on the boundary.
NEAR = (23.7640, 92.7178)
FAR = (24.0900, 92.7178)


class AutoAssignTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=50)

    def ready(self, order: Order) -> Order:
        """Move an order to Ready over HTTP, as a manager would."""
        self.as_admin()
        response = self.client.patch(
            f"/api/orders/{order.id}/status", {"status": Order.PACKING}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.patch(
            f"/api/orders/{order.id}/status", {"status": Order.READY}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        return Order.objects.get(pk=order.id)

    # --- the happy path --------------------------------------------------
    def test_ready_order_is_assigned_and_dispatched(self):
        rider = self.make_rider()
        order = self.ready(self.place_order(self.product))

        self.assertEqual(order.delivery_boy_id, rider.id)
        self.assertEqual(order.status, Order.DISPATCHED)
        # advance_status stamps this; a hand-assigned column would not.
        self.assertIsNotNone(order.dispatched_at)

    def test_nearest_rider_wins(self):
        far = self.make_rider(
            name="Far", phone="+919000000101",
            latitude=FAR[0], longitude=FAR[1], radius=100.0,
        )
        near = self.make_rider(
            name="Near", phone="+919000000102",
            latitude=NEAR[0], longitude=NEAR[1],
        )

        order = self.ready(self.place_order(self.product))

        self.assertEqual(order.delivery_boy_id, near.id)
        self.assertIsNotNone(order.offered_distance_km)
        self.assertLess(order.offered_distance_km, 10)
        self.assertNotEqual(order.delivery_boy_id, far.id)

    def test_assignment_is_audited(self):
        rider = self.make_rider()
        order = self.ready(self.place_order(self.product))

        entry = AuditLog.objects.filter(
            action=AuditLog.ASSIGN, entity_id=order.id
        ).first()
        self.assertIsNotNone(entry)
        self.assertIn(rider.name, entry.summary)
        self.assertTrue(entry.changes["auto"])

    # --- who is skipped --------------------------------------------------
    def test_rider_off_shift_is_skipped(self):
        self.make_rider(available=False)
        order = self.ready(self.place_order(self.product))

        self.assertIsNone(order.delivery_boy_id)
        self.assertEqual(order.status, Order.READY)

    def test_deactivated_rider_is_skipped(self):
        self.make_rider(active=False)
        order = self.ready(self.place_order(self.product))
        self.assertIsNone(order.delivery_boy_id)

    def test_rider_out_of_range_is_skipped(self):
        self.make_rider(latitude=FAR[0], longitude=FAR[1], radius=10.0)
        order = self.ready(self.place_order(self.product))
        self.assertIsNone(order.delivery_boy_id)

    def test_rider_already_carrying_is_skipped(self):
        """One drop at a time. A 15-minute promise does not survive stacking."""
        rider = self.make_rider()
        first = self.ready(self.place_order(self.product))
        self.assertEqual(first.delivery_boy_id, rider.id)

        second = self.ready(self.place_order(self.product))
        self.assertIsNone(second.delivery_boy_id)
        self.assertEqual(second.status, Order.READY)

    def test_rider_who_declined_is_skipped(self):
        rider = self.make_rider()
        order = self.place_order(self.product)
        OrderRejection.objects.create(order=order, rider=rider)

        order = self.ready(order)
        self.assertIsNone(order.delivery_boy_id)

    # --- no rider at all -------------------------------------------------
    def test_no_riders_leaves_the_order_ready(self):
        """The manager's request still succeeds; the order waits in the feed."""
        order = self.ready(self.place_order(self.product))

        self.assertIsNone(order.delivery_boy_id)
        self.assertEqual(order.status, Order.READY)

    def test_unassigned_order_shows_as_stalled_to_the_manager(self):
        self.make_rider(latitude=FAR[0], longitude=FAR[1], radius=1.0)
        order = self.ready(self.place_order(self.product))

        self.as_admin()
        response = self.client.get("/api/orders?stalled=true")
        self.assertEqual(response.status_code, 200)
        self.assertIn(order.id, [row["id"] for row in response.data])

    # --- the hand-back loop ----------------------------------------------
    def test_hand_back_does_not_bounce_to_the_same_rider(self):
        """Dispatched -> Ready by the rider must not re-assign them instantly.

        Without the decline this records, the nearest rider is the one who just
        let go of the order, and it ping-pongs until someone opens the console.
        """
        rider = self.make_rider()
        order = self.ready(self.place_order(self.product))
        self.assertEqual(order.delivery_boy_id, rider.id)

        self.as_rider(rider)
        response = self.client.patch(
            f"/api/orders/{order.id}/status", {"status": Order.READY}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        order.refresh_from_db()
        self.assertIsNone(order.delivery_boy_id)
        self.assertEqual(order.status, Order.READY)
        self.assertTrue(
            OrderRejection.objects.filter(order=order, rider=rider).exists()
        )

    def test_hand_back_goes_to_the_next_rider(self):
        first = self.make_rider(name="First", phone="+919000000201")
        second = self.make_rider(
            name="Second", phone="+919000000202",
            latitude=NEAR[0], longitude=NEAR[1],
        )

        order = self.ready(self.place_order(self.product))
        self.assertEqual(order.delivery_boy_id, first.id)

        self.as_rider(first)
        self.client.patch(
            f"/api/orders/{order.id}/status", {"status": Order.READY}, format="json"
        )

        order.refresh_from_db()
        self.assertEqual(order.delivery_boy_id, second.id)
        self.assertEqual(order.status, Order.DISPATCHED)

    # --- the manager's override still wins -------------------------------
    def test_manager_can_reassign_an_auto_assigned_order(self):
        auto = self.make_rider(name="Auto", phone="+919000000301")
        chosen = self.make_rider(
            name="Chosen", phone="+919000000302",
            latitude=FAR[0], longitude=FAR[1], radius=100.0,
        )

        order = self.ready(self.place_order(self.product))
        self.assertEqual(order.delivery_boy_id, auto.id)

        self.as_admin()
        response = self.client.post(
            f"/api/orders/{order.id}/assign",
            {"delivery_boy_id": chosen.id},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.delivery_boy_id, chosen.id)

    # --- the switch ------------------------------------------------------
    @override_settings(AUTO_ASSIGN_RIDER=False)
    def test_switch_off_falls_back_to_the_pull_feed(self):
        rider = self.make_rider()
        order = self.ready(self.place_order(self.product))

        self.assertIsNone(order.delivery_boy_id)
        self.assertEqual(order.status, Order.READY)

        # And the rider can still claim it the old way.
        self.as_rider(rider)
        response = self.client.post(f"/api/orders/{order.id}/accept")
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.delivery_boy_id, rider.id)


class RiderFeedTests(APITestBase):
    """The pull feed still has to work -- it is the fallback, not dead code."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=50)

    @override_settings(AUTO_ASSIGN_RIDER=False)
    def test_unassigned_ready_order_appears_in_the_feed(self):
        rider = self.make_rider()
        order = self.place_order(self.product)
        self.advance(order, Order.PACKING, Order.READY)

        self.as_rider(rider)
        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            order.id, [row["id"] for row in response.data["incoming_orders"]]
        )
