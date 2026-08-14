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


class OfferPrivacyTests(APITestBase):
    """An offer is not a job, and must not read like one.

    `incoming_orders` lists orders belonging to nobody, shown to *every*
    available rider in range. Serialising them with the same class as the
    rider's own work handed out the customer's name, phone and address for
    orders that rider would never take — and, worse, the `tracking_token`,
    which is the sole credential on the public cancel endpoint. Any rider on
    shift could cancel any bagged order in town.
    """

    #: Everything that identifies the customer, or acts on their behalf.
    FORBIDDEN = [
        "tracking_token",
        "customer_name",
        "customer_phone",
        "customer_address",
        "delivery_notes",
        "customer_latitude",
        "customer_longitude",
    ]

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=50)
        self.rider = self.make_rider()
        self.order = self.place_order(self.product, 2)
        self.advance(self.order, Order.PACKING, Order.READY)

    def dashboard(self):
        self.as_rider(self.rider)
        response = self.client.get(f"/api/delivery/{self.rider.id}/dashboard")
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_offers_carry_nothing_identifying(self):
        offer = self.dashboard()["incoming_orders"][0]

        for field in self.FORBIDDEN:
            self.assertNotIn(field, offer, f"{field} leaked into the offer feed")

    def test_offers_carry_what_the_decision_needs(self):
        """Stripping the feed must not leave a rider unable to judge a job."""
        offer = self.dashboard()["incoming_orders"][0]

        self.assertEqual(offer["id"], self.order.id)
        self.assertEqual(offer["item_count"], 2)
        self.assertMoney(offer["grand_total"], str(self.order.grand_total))
        self.assertIn("offered_distance_km", offer)
        self.assertIn("minutes_remaining", offer)

    def test_area_is_a_locality_not_a_doorstep(self):
        """The address is reduced to its last segment, never the house number."""
        offer = self.dashboard()["incoming_orders"][0]

        # base.py addresses this order to "House 42, Chanmari, Aizawl".
        self.assertEqual(offer["area"], "Aizawl")
        self.assertNotIn("42", offer["area"])

    def test_landmark_is_preferred_over_the_address(self):
        self.order.customer_landmark = "Near Ramhlun Bus Stop"
        self.order.save(update_fields=["customer_landmark"])

        offer = self.dashboard()["incoming_orders"][0]

        self.assertEqual(offer["area"], "Near Ramhlun Bus Stop")

    def test_accepting_the_order_reveals_the_customer(self):
        """The rider holding the bag needs the door — that is the whole point.

        This is the other half of the invariant: the detail is withheld until
        the rider has the job, not withheld outright.
        """
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        active = self.dashboard()["active_order"]

        self.assertEqual(active["customer_phone"], self.order.customer_phone)
        self.assertEqual(active["customer_address"], self.order.customer_address)

    def test_the_token_is_absent_even_once_the_order_is_theirs(self):
        """`tracking_token` is the customer's cancel credential, not the rider's.

        Nothing in the admin console or the rider app has ever read it off this
        serializer; it reaches the customer through OrderTrackingSerializer.
        """
        self.as_rider(self.rider)
        self.client.post(f"/api/orders/{self.order.id}/accept")

        active = self.dashboard()["active_order"]

        self.assertNotIn("tracking_token", active)


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
