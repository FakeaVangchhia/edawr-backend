"""The order state machine, and who is allowed to move it."""

from django.test import override_settings

from api.models import Order, OrderRejection
from api.tests.base import APITestBase


class StateMachineTests(APITestBase):
    """The model's own rules, independent of any HTTP layer."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 2)

    def test_the_happy_path_is_legal(self):
        for target in [Order.PACKING, Order.READY, Order.DISPATCHED, Order.DELIVERED]:
            with self.subTest(target=target):
                changed = self.order.advance_status(target)
                self.assertIn("status", changed)

    def test_illegal_jumps_raise(self):
        for target in [Order.DISPATCHED, Order.DELIVERED, Order.READY]:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    Order(status=Order.PLACED).advance_status(target)

    def test_terminal_states_have_no_exits(self):
        for terminal in [Order.DELIVERED, Order.CANCELLED]:
            for target in [Order.PLACED, Order.PACKING, Order.READY, Order.DISPATCHED]:
                with self.subTest(terminal=terminal, target=target):
                    with self.assertRaises(ValueError):
                        Order(status=terminal).advance_status(target)

    def test_transition_to_the_current_state_is_a_no_op(self):
        self.assertEqual(self.order.advance_status(Order.PLACED), [])

    def test_timestamps_are_stamped_once(self):
        self.advance(self.order, Order.PACKING, Order.READY, Order.DISPATCHED)
        first_dispatch = self.order.dispatched_at
        self.assertIsNotNone(first_dispatch)

        # Rider hands it back, another picks it up: the moment it *first* left
        # the store is what the fulfilment numbers should measure.
        self.advance(self.order, Order.READY, Order.DISPATCHED)

        self.assertEqual(self.order.dispatched_at, first_dispatch)

    def test_minutes_remaining_counts_down_and_floors_at_zero(self):
        self.assertGreater(self.order.minutes_remaining, 0)

        self.order.promised_minutes = 0
        self.assertEqual(self.order.minutes_remaining, 0)

    def test_delivered_orders_have_no_countdown(self):
        self.advance(self.order, Order.PACKING, Order.READY, Order.DISPATCHED, Order.DELIVERED)
        self.assertEqual(self.order.minutes_remaining, 0)
        self.assertFalse(self.order.is_late)


class ManagerStatusTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 2)
        self.url = f"/api/orders/{self.order.id}/status"
        self.as_admin()

    def test_manager_can_move_an_order_through_packing(self):
        response = self.client.patch(self.url, {"status": Order.PACKING}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.PACKING)
        self.assertIsNone(response.data["packed_at"])

    def test_packed_at_is_stamped_at_ready(self):
        self.client.patch(self.url, {"status": Order.PACKING}, format="json")
        response = self.client.patch(self.url, {"status": Order.READY}, format="json")

        self.assertIsNotNone(response.data["packed_at"])

    def test_illegal_transition_is_a_409(self):
        response = self.client.patch(self.url, {"status": Order.DELIVERED}, format="json")

        self.assertEqual(response.status_code, 409)

    def test_manager_cannot_dispatch_directly(self):
        """Dispatch means a specific rider physically took it."""
        response = self.client.patch(self.url, {"status": Order.DISPATCHED}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_manager_cancellation_restores_stock(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)

        response = self.client.patch(self.url, {"status": Order.CANCELLED}, format="json")

        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_unknown_status_is_a_400(self):
        response = self.client.patch(self.url, {"status": "Teleported"}, format="json")

        self.assertEqual(response.status_code, 400)


class RiderOrderTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 2)
        self.advance(self.order, Order.PACKING, Order.READY)
        self.rider = self.make_rider()
        self.other_rider = self.make_rider(name="Rider Two", phone="+919000000003")

    def test_rider_can_accept_a_ready_order(self):
        self.as_rider(self.rider)

        response = self.client.post(f"/api/orders/{self.order.id}/accept")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.DISPATCHED)
        self.assertEqual(response.data["delivery_boy_id"], self.rider.id)

    def test_rider_cannot_accept_an_order_that_is_not_ready(self):
        self.advance(self.order, Order.PACKING)
        self.as_rider(self.rider)

        response = self.client.post(f"/api/orders/{self.order.id}/accept")

        self.assertEqual(response.status_code, 409)

    def test_second_rider_to_accept_gets_a_conflict(self):
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        self.as_rider(self.other_rider)
        response = self.client.post(f"/api/orders/{self.order.id}/accept")

        self.assertEqual(response.status_code, 409)
        self.order.refresh_from_db()
        self.assertEqual(self.order.delivery_boy_id, self.rider.id)

    def test_rider_can_deliver_their_own_order(self):
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        response = self.client.patch(
            f"/api/orders/{self.order.id}/status", {"status": Order.DELIVERED}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["delivered_at"])
        self.assertIsNotNone(response.data["fulfilment_minutes"])

    def test_rider_cannot_touch_another_riders_order(self):
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        self.as_rider(self.other_rider)
        response = self.client.patch(
            f"/api/orders/{self.order.id}/status", {"status": Order.DELIVERED}, format="json"
        )

        self.assertEqual(response.status_code, 403)

    def test_rider_cannot_cancel(self):
        """Cancelling means a refund conversation; that is the store's call."""
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        response = self.client.patch(
            f"/api/orders/{self.order.id}/status", {"status": Order.CANCELLED}, format="json"
        )

        self.assertEqual(response.status_code, 403)

    def test_rider_can_hand_an_order_back(self):
        """Handing back must release the rider, whatever happens next.

        With automatic dispatch on, "back" means straight to the next rider in
        range rather than into a pool — there is a second rider in this fixture,
        so the order leaves as Dispatched to them. What has to hold either way is
        that it is no longer *this* rider's: an order that looks unclaimed while
        still being held makes every accept fail with a 409 nobody can explain.
        """
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        response = self.client.patch(
            f"/api/orders/{self.order.id}/status", {"status": Order.READY}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.data["delivery_boy_id"], self.rider.id)
        self.assertEqual(response.data["delivery_boy_id"], self.other_rider.id)

    @override_settings(AUTO_ASSIGN_RIDER=False)
    def test_hand_back_returns_it_to_the_pool_when_dispatch_is_off(self):
        """The pull-feed contract, which is still what runs with the switch off."""
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        response = self.client.patch(
            f"/api/orders/{self.order.id}/status", {"status": Order.READY}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.READY)
        self.assertIsNone(response.data["delivery_boy_id"])


class RejectionTests(APITestBase):
    """The Reject button used to do nothing at all."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 2)
        self.advance(self.order, Order.PACKING, Order.READY)
        self.rider = self.make_rider()
        self.other_rider = self.make_rider(name="Rider Two", phone="+919000000003")

    def test_rejecting_records_the_decline(self):
        self.as_rider(self.rider)

        response = self.client.post(f"/api/orders/{self.order.id}/reject")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            OrderRejection.objects.filter(order=self.order, rider=self.rider).exists()
        )

    def test_rejecting_twice_is_idempotent(self):
        self.as_rider(self.rider)

        self.client.post(f"/api/orders/{self.order.id}/reject")
        response = self.client.post(f"/api/orders/{self.order.id}/reject")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(OrderRejection.objects.filter(order=self.order).count(), 1)

    def test_a_rejected_order_leaves_that_riders_feed(self):
        self.as_rider(self.rider)
        self.assertEqual(len(self._feed(self.rider)), 1)

        self.client.post(f"/api/orders/{self.order.id}/reject")

        self.assertEqual(len(self._feed(self.rider)), 0)

    def test_a_rejected_order_stays_in_everyone_elses_feed(self):
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/reject")

        self.assertEqual(len(self._feed(self.other_rider)), 1)

    def test_cannot_reject_an_order_you_already_accepted(self):
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        response = self.client.post(f"/api/orders/{self.order.id}/reject")

        self.assertEqual(response.status_code, 409)

    def test_cannot_reject_a_finished_order(self):
        self.advance(self.order, Order.DISPATCHED, Order.DELIVERED)
        self.as_rider(self.rider)

        response = self.client.post(f"/api/orders/{self.order.id}/reject")

        self.assertEqual(response.status_code, 409)

    def test_manager_can_see_orders_every_rider_declined(self):
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/reject")
        self.as_rider(self.other_rider)
        self.client.post(f"/api/orders/{self.order.id}/reject")

        self.as_admin()
        response = self.client.get("/api/orders?stalled=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.order.id])

    def test_stalled_filter_is_off_by_default_and_respects_false(self):
        """`bool("false")` is True — the classic always-on filter bug."""
        self.as_admin()

        self.assertEqual(len(self.client.get("/api/orders").data), 1)
        self.assertEqual(len(self.client.get("/api/orders?stalled=false").data), 1)

    def _feed(self, rider):
        self.as_rider(rider)
        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")
        self.assertEqual(response.status_code, 200)
        return response.data["incoming_orders"]


class ManagerAssignmentTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 2)
        self.rider = self.make_rider()
        self.as_admin()

    def test_assigning_walks_a_placed_order_all_the_way_to_dispatched(self):
        response = self.client.post(
            f"/api/orders/{self.order.id}/assign",
            {"delivery_boy_id": self.rider.id},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.DISPATCHED)
        self.assertEqual(response.data["delivery_boy_id"], self.rider.id)
        self.assertIsNotNone(response.data["packed_at"])
        self.assertIsNotNone(response.data["dispatched_at"])

    def test_assignment_overrides_a_rejection(self):
        """A manager who has just spoken to a rider knows more than the queue."""
        OrderRejection.objects.create(order=self.order, rider=self.rider)

        response = self.client.post(
            f"/api/orders/{self.order.id}/assign",
            {"delivery_boy_id": self.rider.id},
            format="json",
        )

        self.assertEqual(response.status_code, 200)

    def test_cannot_assign_a_finished_order(self):
        self.advance(self.order, Order.PACKING, Order.READY, Order.DISPATCHED, Order.DELIVERED)

        response = self.client.post(
            f"/api/orders/{self.order.id}/assign",
            {"delivery_boy_id": self.rider.id},
            format="json",
        )

        self.assertEqual(response.status_code, 409)

    def test_unknown_rider_is_a_400(self):
        response = self.client.post(
            f"/api/orders/{self.order.id}/assign",
            {"delivery_boy_id": 999999},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_inactive_rider_cannot_be_assigned(self):
        self.rider.is_active = False
        self.rider.save()

        response = self.client.post(
            f"/api/orders/{self.order.id}/assign",
            {"delivery_boy_id": self.rider.id},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
