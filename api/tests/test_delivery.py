"""The rider's feed: who gets offered what."""

from api.models import Order, OrderRejection
from api.tests.base import APITestBase


class DashboardTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=50)
        self.rider = self.make_rider(latitude=23.7272, longitude=92.7178, radius=10.0)
        self.order = self.place_order(self.product, 2)
        self.advance(self.order, Order.PACKING, Order.READY)

    def feed(self, rider=None):
        rider = rider or self.rider
        self.as_rider(rider)
        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_ready_orders_appear_in_the_feed(self):
        self.assertEqual(len(self.feed()["incoming_orders"]), 1)

    def test_orders_that_are_not_ready_do_not_appear(self):
        other = self.place_order(self.product, 1)  # still Placed

        incoming = self.feed()["incoming_orders"]

        self.assertEqual([row["id"] for row in incoming], [self.order.id])
        self.assertNotIn(other.id, [row["id"] for row in incoming])

    def test_a_rider_who_is_unavailable_is_offered_nothing(self):
        self.rider.is_available = False
        self.rider.save()

        data = self.feed()

        self.assertEqual(data["incoming_orders"], [])
        self.assertFalse(data["is_available"])

    def test_a_rider_already_carrying_an_order_is_offered_nothing(self):
        """A 10-minute promise does not survive stacking two drops on one rider."""
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        second = self.place_order(self.product, 1)
        self.advance(second, Order.PACKING, Order.READY)

        data = self.feed()

        self.assertEqual(data["incoming_orders"], [])
        self.assertEqual(data["active_order"]["id"], self.order.id)

    def test_orders_outside_the_service_radius_are_excluded(self):
        far_rider = self.make_rider(
            name="Far", phone="+919000000009", latitude=25.0, longitude=95.0, radius=1.0
        )

        self.assertEqual(self.feed(far_rider)["incoming_orders"], [])

    def test_feed_is_sorted_nearest_first(self):
        near = self.place_order(
            self.product, 1, customer_latitude=23.7273, customer_longitude=92.7179
        )
        self.advance(near, Order.PACKING, Order.READY)
        far = self.place_order(
            self.product, 1, customer_latitude=23.8000, customer_longitude=92.8000
        )
        self.advance(far, Order.PACKING, Order.READY)

        incoming = self.feed()["incoming_orders"]
        distances = [row["offered_distance_km"] for row in incoming]

        self.assertEqual(distances, sorted(distances))

    def test_rejected_orders_are_excluded(self):
        OrderRejection.objects.create(order=self.order, rider=self.rider)

        self.assertEqual(self.feed()["incoming_orders"], [])

    def test_an_order_claimed_by_someone_else_is_excluded(self):
        other = self.make_rider(name="Other", phone="+919000000003")
        self.as_rider(other)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        self.assertEqual(self.feed()["incoming_orders"], [])

    def test_recent_shows_this_riders_deliveries_only(self):
        other = self.make_rider(name="Other", phone="+919000000003")
        mine = self.place_order(self.product, 1)
        self.advance(mine, Order.PACKING, Order.READY)
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{mine.id}/accept")
        self.client.patch(
            f"/api/orders/{mine.id}/status", {"status": Order.DELIVERED}, format="json"
        )

        theirs = self.place_order(self.product, 1)
        self.advance(theirs, Order.PACKING, Order.READY)
        self.as_rider(other)
        self.client.post(f"/api/orders/{theirs.id}/accept")
        self.client.patch(
            f"/api/orders/{theirs.id}/status", {"status": Order.DELIVERED}, format="json"
        )

        recent = self.feed(self.rider)["recent_orders"]

        self.assertEqual([row["id"] for row in recent], [mine.id])

    def test_a_rider_cannot_read_another_riders_dashboard(self):
        """Walking this integer used to return every customer's address."""
        other = self.make_rider(name="Other", phone="+919000000003")
        self.as_rider(self.rider)

        response = self.client.get(f"/api/delivery/{other.id}/dashboard")

        self.assertEqual(response.status_code, 403)

    def test_dashboard_does_not_write(self):
        """Distances are computed for display and must not be persisted."""
        self.feed()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.offered_distance_km)


class AvailabilityTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.rider = self.make_rider()

    def test_rider_can_go_offline_and_back_on(self):
        self.as_rider(self.rider)

        response = self.client.patch(
            "/api/delivery/availability", {"is_available": False}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_available"])

        self.rider.refresh_from_db()
        self.assertFalse(self.rider.is_available)

    def test_availability_cannot_reactivate_a_dismissed_rider(self):
        """is_active is the manager's switch; is_available is the rider's."""
        self.as_rider(self.rider)
        self.rider.is_active = False
        self.rider.save()

        response = self.client.patch(
            "/api/delivery/availability", {"is_available": True}, format="json"
        )

        self.assertEqual(response.status_code, 401)

    def test_roster_requires_an_admin_token(self):
        """A rider must not be able to read every other rider's phone number —
        that is half of the sign-in credential."""
        self.as_rider(self.rider)

        self.assertEqual(self.client.get("/api/delivery/riders").status_code, 403)

        self.as_anonymous()
        self.assertEqual(self.client.get("/api/delivery/riders").status_code, 401)

        self.as_admin()
        self.assertEqual(self.client.get("/api/delivery/riders").status_code, 200)
