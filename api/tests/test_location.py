"""Live position tracking.

The rules under test, in the order they matter:

1. **A rider's position comes from their token.** There is no rider id in the
   path or the body of the report endpoint, so there is nothing to walk and
   nothing to spoof.
2. **The trail is written only during a delivery.** A rider between drops
   updates their current position and leaves no history behind.
3. **The customer sees a rider only while one is carrying their order**, and
   only if the fix is fresh. Not before Dispatched, not after it ends, not when
   the phone has gone quiet.
4. **A rider's home base never reaches a customer.** `base_latitude` is where a
   member of staff lives.
5. **An order with no coordinates yields a null distance, never zero.** This is
   the same invariant `dispatch._rank` carries, and the reason it exists is that
   the columns once defaulted to the store's own position and made every rider
   read as 0.00 km away.

Coordinates are real Aizawl ones, matching `test_dispatch.py`, so a distance
assertion compares two genuinely different points.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from api.models import (
    Order,
    OrderCustomerLocation,
    OrderLocationPing,
    RiderLocation,
)
from api.tests.base import APITestBase

# The store and the default customer sit here; see base.STORE_LATITUDE.
STORE = (23.7272, 92.7178)
# ~4 km north — a plausible "rider is on the way" position.
NEAR = (23.7640, 92.7178)
FAR = (24.0900, 92.7178)


class LocationTestBase(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=50)

    def report(self, latitude: float, longitude: float, **extra):
        """POST one position fix as the currently authenticated rider."""
        body = {"latitude": latitude, "longitude": longitude}
        body.update(extra)
        return self.client.post("/api/delivery/location", body, format="json")

    def dispatched_order(self, rider, **order_kwargs) -> Order:
        """An order in this rider's hands, walked there through the machine."""
        order = self.place_order(self.product, **order_kwargs)
        order.delivery_boy = rider
        order.save(update_fields=["delivery_boy"])
        return self.advance(order, Order.PACKING, Order.READY, Order.DISPATCHED)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
class RiderPositionReportTests(LocationTestBase):
    def test_a_rider_reports_their_own_position(self):
        rider = self.make_rider()
        self.as_rider(rider)

        response = self.report(*NEAR, accuracy_m=12.5, speed_kmh=18.0, heading=95.0)

        self.assertEqual(response.status_code, 200, response.data)
        stored = RiderLocation.objects.get(rider=rider)
        self.assertAlmostEqual(stored.latitude, NEAR[0])
        self.assertAlmostEqual(stored.longitude, NEAR[1])
        self.assertEqual(stored.accuracy_m, 12.5)
        self.assertEqual(stored.heading, 95.0)

    def test_the_endpoint_accepts_no_rider_id(self):
        """The body cannot name a rider, so one rider cannot move another.

        A `rider` key in the payload is simply not a field on the serializer.
        The fix lands on the caller regardless of what they claim, which is the
        same guarantee `accept`, `reject` and `status` give by taking the rider
        from the token.
        """
        caller = self.make_rider()
        victim = self.make_rider(name="Rider Two", phone="+919000000003")
        self.as_rider(caller)

        response = self.report(*NEAR, rider=victim.id, rider_id=victim.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(RiderLocation.objects.filter(rider=caller).exists())
        self.assertFalse(RiderLocation.objects.filter(rider=victim).exists())

    def test_reporting_twice_overwrites_rather_than_accumulating(self):
        rider = self.make_rider()
        self.as_rider(rider)

        self.report(*STORE)
        self.report(*NEAR)

        self.assertEqual(RiderLocation.objects.filter(rider=rider).count(), 1)
        self.assertAlmostEqual(
            RiderLocation.objects.get(rider=rider).latitude, NEAR[0]
        )

    def test_an_empty_fix_at_null_island_is_refused(self):
        """(0, 0) is what a failed GPS lock serialises to, not a position."""
        self.as_rider(self.make_rider())

        response = self.report(0, 0)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RiderLocation.objects.exists())

    def test_out_of_range_coordinates_are_refused(self):
        self.as_rider(self.make_rider())

        self.assertEqual(self.report(91.0, 92.7178).status_code, 400)
        self.assertEqual(self.report(23.7272, 181.0).status_code, 400)
        self.assertFalse(RiderLocation.objects.exists())

    def test_an_anonymous_caller_cannot_report_a_position(self):
        self.as_anonymous()
        self.assertEqual(self.report(*NEAR).status_code, 401)

    def test_a_console_token_is_forbidden_rather_than_unauthorised(self):
        """403, not 401: we know who this is, they simply may not do it.

        The distinction is the one `test_auth.py` guards — a 401 is what makes a
        client clear its stored session, and an admin poking a rider route must
        not be signed out for it.
        """
        self.as_admin()
        self.assertEqual(self.report(*NEAR).status_code, 403)


class BreadcrumbTests(LocationTestBase):
    def test_a_fix_during_a_delivery_leaves_a_trail(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)

        self.report(*NEAR)

        pings = OrderLocationPing.objects.filter(order=order)
        self.assertEqual(pings.count(), 1)
        self.assertEqual(pings.first().rider_id, rider.id)

    def test_the_response_names_the_order_the_fix_belongs_to(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)

        response = self.report(*NEAR)

        self.assertEqual(response.data["order_id"], order.id)

    def test_a_rider_between_drops_leaves_no_trail(self):
        """Current position, yes. History, no.

        This is what makes "we track a rider during a delivery" a statement
        about the stored data rather than about the app's intentions.
        """
        rider = self.make_rider()
        self.as_rider(rider)

        response = self.report(*NEAR)

        self.assertIsNone(response.data["order_id"])
        self.assertTrue(RiderLocation.objects.filter(rider=rider).exists())
        self.assertEqual(OrderLocationPing.objects.count(), 0)

    def test_the_trail_stops_when_the_order_does(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)

        self.advance(order, Order.DELIVERED)
        self.report(*STORE)

        # One ping from while it was out, and nothing after it was handed over.
        self.assertEqual(OrderLocationPing.objects.filter(order=order).count(), 1)


class ClockSkewTests(LocationTestBase):
    def test_a_wrong_handset_clock_does_not_make_a_fix_stale(self):
        """Freshness is the server's clock, never the client's.

        A handset reporting a timestamp an hour old is a handset with a wrong
        clock, not an old position — and if staleness were read from that field,
        it would be read from a number the caller controls.
        """
        rider = self.make_rider()
        self.as_rider(rider)
        stale_by_the_device = (timezone.now() - timedelta(hours=1)).isoformat()

        self.report(*NEAR, recorded_at=stale_by_the_device)

        stored = RiderLocation.objects.get(rider=rider)
        self.assertIsNotNone(stored.recorded_at)
        self.assertFalse(stored.is_stale)


# --------------------------------------------------------------------------
# The console
# --------------------------------------------------------------------------
class ConsoleRosterTests(LocationTestBase):
    def test_a_manager_sees_every_active_rider(self):
        tracked = self.make_rider(name="Ramthar")
        self.make_rider(name="Zoram", phone="+919000000003")
        self.as_rider(tracked)
        self.report(*NEAR)

        self.as_manager()
        response = self.client.get("/api/delivery/locations")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 2)

    def test_a_rider_who_has_never_reported_appears_with_nulls(self):
        """A silent gap in the roster would read as "not working"."""
        self.make_rider(name="Zoram")

        self.as_manager()
        row = self.client.get("/api/delivery/locations").data[0]

        self.assertIsNone(row["latitude"])
        self.assertIsNone(row["received_at"])
        self.assertTrue(row["is_stale"])

    @override_settings(LOCATION_STALE_SECONDS=90)
    def test_a_stale_position_is_flagged_but_still_returned(self):
        """A manager can act on "last seen 4 minutes ago near Chanmari"."""
        rider = self.make_rider()
        self.as_rider(rider)
        self.report(*NEAR)
        RiderLocation.objects.filter(rider=rider).update(
            received_at=timezone.now() - timedelta(minutes=4)
        )

        self.as_manager()
        row = self.client.get("/api/delivery/locations").data[0]

        self.assertTrue(row["is_stale"])
        self.assertIsNotNone(row["latitude"])
        self.assertGreaterEqual(row["age_seconds"], 240)

    def test_the_roster_never_carries_a_riders_home_base(self):
        self.as_rider(self.make_rider())
        self.report(*NEAR)

        self.as_manager()
        row = self.client.get("/api/delivery/locations").data[0]

        self.assertNotIn("base_latitude", row)
        self.assertNotIn("base_longitude", row)

    def test_a_rider_may_not_read_the_roster(self):
        self.as_rider(self.make_rider())
        self.assertEqual(self.client.get("/api/delivery/locations").status_code, 403)

    def test_an_anonymous_caller_may_not_read_the_roster(self):
        self.as_anonymous()
        self.assertEqual(self.client.get("/api/delivery/locations").status_code, 401)


# --------------------------------------------------------------------------
# The customer's view — the privacy boundary
# --------------------------------------------------------------------------
class TrackedRiderLocationTests(LocationTestBase):
    def track(self, order: Order):
        self.as_anonymous()
        return self.client.get(
            f"/api/store/orders/{order.tracking_token}/rider-location"
        )

    def test_a_dispatched_order_shows_a_fresh_rider_position(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)

        response = self.track(order)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data["rider"])
        self.assertAlmostEqual(response.data["rider"]["latitude"], NEAR[0])

    def test_nothing_is_shown_before_the_order_is_dispatched(self):
        rider = self.make_rider()
        self.as_rider(rider)
        self.report(*NEAR)
        order = self.place_order(self.product)

        self.assertIsNone(self.track(order).data["rider"])

    def test_nothing_is_shown_once_the_order_has_been_delivered(self):
        """The tracking token is not a way to watch a rider's whole shift."""
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)

        self.advance(order, Order.DELIVERED)

        self.assertIsNone(self.track(order).data["rider"])

    def test_nothing_is_shown_once_the_delivery_has_failed(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)

        self.advance(order, Order.FAILED)

        self.assertIsNone(self.track(order).data["rider"])

    @override_settings(LOCATION_STALE_SECONDS=90)
    def test_a_stale_position_is_hidden_rather_than_shown_as_live(self):
        """Worse than an empty map: a customer at the window watching nobody."""
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)
        RiderLocation.objects.filter(rider=rider).update(
            received_at=timezone.now() - timedelta(minutes=11)
        )

        self.assertIsNone(self.track(order).data["rider"])

    def test_a_rider_who_has_never_reported_shows_nothing(self):
        order = self.dispatched_order(self.make_rider())
        self.assertIsNone(self.track(order).data["rider"])

    def test_an_unknown_token_is_a_404(self):
        self.as_anonymous()
        response = self.client.get("/api/store/orders/not-a-real-token/rider-location")
        self.assertEqual(response.status_code, 404)

    def test_the_distance_is_measured_to_the_delivery_address(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)

        distance = self.track(order).data["rider"]["distance_km"]

        # NEAR is ~4 km north of the store, which is where this order goes.
        self.assertIsNotNone(distance)
        self.assertGreater(distance, 3.5)
        self.assertLess(distance, 4.5)

    def test_an_order_with_no_coordinates_has_a_null_distance_not_zero(self):
        """The load-bearing invariant. Null and zero are different facts.

        `dispatch._rank` carries the same rule, and the comment on
        `Order.customer_latitude` records what happened when they were
        conflated: every rider measured 0.00 km away and the app said so.
        """
        rider = self.make_rider()
        order = self.dispatched_order(
            rider, customer_latitude=None, customer_longitude=None
        )
        self.as_rider(rider)
        self.report(*NEAR)

        payload = self.track(order).data["rider"]

        self.assertIsNotNone(payload)
        self.assertIsNone(payload["distance_km"])

    def test_the_customer_never_learns_the_riders_identity_or_home(self):
        """Identity is `OrderTrackingSerializer`'s decision, not this route's."""
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)

        payload = self.track(order).data["rider"]

        for forbidden in (
            "id",
            "name",
            "phone",
            "base_latitude",
            "base_longitude",
            "accuracy_m",
        ):
            self.assertNotIn(forbidden, payload)

    def test_one_token_does_not_reach_another_orders_rider(self):
        rider = self.make_rider()
        mine = self.dispatched_order(rider)
        self.as_rider(rider)
        self.report(*NEAR)

        other_rider = self.make_rider(name="Rider Two", phone="+919000000003")
        theirs = self.dispatched_order(other_rider)

        # My token shows my rider; it says nothing about the other order's,
        # which has never reported.
        self.assertIsNotNone(self.track(mine).data["rider"])
        self.assertIsNone(self.track(theirs).data["rider"])


# --------------------------------------------------------------------------
# The customer's own position
# --------------------------------------------------------------------------
class CustomerPositionTests(LocationTestBase):
    def share(self, order: Order, latitude=NEAR[0], longitude=NEAR[1], **extra):
        self.as_anonymous()
        body = {"latitude": latitude, "longitude": longitude}
        body.update(extra)
        return self.client.post(
            f"/api/store/orders/{order.tracking_token}/location", body, format="json"
        )

    def test_the_tracking_token_is_the_whole_credential(self):
        order = self.place_order(self.product)

        response = self.share(order, accuracy_m=8.0)

        self.assertEqual(response.status_code, 204, getattr(response, "data", None))
        stored = OrderCustomerLocation.objects.get(order=order)
        self.assertAlmostEqual(stored.latitude, NEAR[0])
        self.assertEqual(stored.accuracy_m, 8.0)

    def test_it_never_rewrites_the_checkout_position(self):
        """A fix taken from a moving car must not move the delivery address."""
        order = self.place_order(self.product)
        original = (order.customer_latitude, order.customer_longitude)

        self.share(order, *FAR)

        order.refresh_from_db()
        self.assertEqual((order.customer_latitude, order.customer_longitude), original)

    def test_an_ended_order_is_a_conflict_not_a_bad_request(self):
        order = self.place_order(self.product)
        self.advance(order, Order.CANCELLED)

        response = self.share(order)

        self.assertEqual(response.status_code, 409)

    def test_an_empty_fix_is_refused(self):
        order = self.place_order(self.product)
        self.assertEqual(self.share(order, 0, 0).status_code, 400)

    def test_an_unknown_token_is_a_404(self):
        self.as_anonymous()
        response = self.client.post(
            "/api/store/orders/not-a-real-token/location",
            {"latitude": NEAR[0], "longitude": NEAR[1]},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_the_carrying_rider_sees_it_on_their_dashboard(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.share(order)

        self.as_rider(rider)
        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")

        self.assertEqual(response.status_code, 200, response.data)
        shared = response.data["customer_location"]
        self.assertIsNotNone(shared)
        self.assertAlmostEqual(shared["latitude"], NEAR[0])

    def test_a_rider_carrying_nothing_sees_no_customer_position(self):
        rider = self.make_rider()
        other = self.make_rider(name="Rider Two", phone="+919000000003")
        self.share(self.dispatched_order(other))

        self.as_rider(rider)
        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")

        self.assertIsNone(response.data["customer_location"])


class CustomerPositionLifecycleTests(LocationTestBase):
    def test_reaching_a_terminal_status_deletes_the_position(self):
        """In `advance_status`, so no route to Delivered can skip it."""
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.share_position(order)
        self.assertTrue(OrderCustomerLocation.objects.filter(order=order).exists())

        self.advance(order, Order.DELIVERED)

        self.assertFalse(OrderCustomerLocation.objects.filter(order=order).exists())

    def test_a_cancellation_deletes_it_too(self):
        order = self.place_order(self.product)
        self.share_position(order)

        self.advance(order, Order.CANCELLED)

        self.assertFalse(OrderCustomerLocation.objects.filter(order=order).exists())

    def test_a_failed_delivery_deletes_it_too(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.share_position(order)

        self.advance(order, Order.FAILED)

        self.assertFalse(OrderCustomerLocation.objects.filter(order=order).exists())

    def share_position(self, order: Order) -> None:
        self.as_anonymous()
        response = self.client.post(
            f"/api/store/orders/{order.tracking_token}/location",
            {"latitude": NEAR[0], "longitude": NEAR[1]},
            format="json",
        )
        assert response.status_code == 204, response.data


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------
class PruneTests(LocationTestBase):
    def stale_ping(self, order: Order, rider, days: int) -> OrderLocationPing:
        ping = OrderLocationPing.objects.create(
            order=order, rider=rider, latitude=NEAR[0], longitude=NEAR[1]
        )
        OrderLocationPing.objects.filter(pk=ping.pk).update(
            received_at=timezone.now() - timedelta(days=days)
        )
        return ping

    @override_settings(LOCATION_PING_RETENTION_DAYS=30)
    def test_expired_breadcrumbs_are_deleted_and_recent_ones_kept(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.stale_ping(order, rider, days=45)
        self.stale_ping(order, rider, days=2)

        call_command("prune_locations")

        self.assertEqual(OrderLocationPing.objects.count(), 1)

    @override_settings(LOCATION_PING_RETENTION_DAYS=30)
    def test_a_dry_run_deletes_nothing(self):
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        self.stale_ping(order, rider, days=45)

        call_command("prune_locations", dry_run=True)

        self.assertEqual(OrderLocationPing.objects.count(), 1)

    def test_a_customer_position_on_an_ended_order_is_swept_up(self):
        """The backstop for anything that ends an order without the machine."""
        rider = self.make_rider()
        order = self.dispatched_order(rider)
        OrderCustomerLocation.objects.create(
            order=order, latitude=NEAR[0], longitude=NEAR[1]
        )
        # A bulk update bypasses advance_status entirely — exactly the case this
        # sweep exists for.
        Order.objects.filter(pk=order.pk).update(status=Order.DELIVERED)

        call_command("prune_locations")

        self.assertFalse(OrderCustomerLocation.objects.exists())
