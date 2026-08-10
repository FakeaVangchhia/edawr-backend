"""Rider-facing endpoints for the mobile app.

Neither is public any more. The dashboard requires a rider token and serves only
that rider's own feed; the roster requires an admin token and exists for manager
tooling. Riders reach the API by signing in at /api/auth/rider/login.

**Route ordering is no longer a hazard.** FastAPI matched routes top to bottom
and took the first hit, so `GET /api/delivery/{id}` registered above
`GET /api/delivery/riders` would swallow the literal string "riders". Django
matches top to bottom too, *but* the `<int:...>` path converter refuses to match
non-numeric segments, so "riders" can never be mistaken for an id. Keeping
literal paths above parameterised ones in `api/urls.py` is still the tidier
habit; it is just no longer load-bearing here.
"""

import math

from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import Order, User
from api.permissions import IsAdmin, IsRider
from api.serializers import DeliveryDashboardSerializer, UserSerializer

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class RiderListView(APIView):
    """GET /api/delivery/riders — the rider roster, for managers.

    **No longer public.** It used to back the mobile login screen's "pick your
    profile" list, which meant anyone who could reach the host got every rider's
    name, phone number and home coordinates — the phone number being half of the
    sign-in credential. Riders now authenticate with phone + PIN and never read
    this, so it exists only for manager tooling and requires an admin token.
    """

    permission_classes = [IsAdmin]

    @extend_schema(responses=UserSerializer(many=True))
    def get(self, request):
        riders = User.objects.filter(role=User.DELIVERY).order_by("id")
        return Response(UserSerializer(riders, many=True).data)


class RiderDashboardView(APIView):
    """A rider's own feed. The `delivery_id` in the path must be the caller."""

    permission_classes = [IsRider]

    @extend_schema(responses=DeliveryDashboardSerializer)
    def get(self, request, delivery_id: int):
        """GET /api/delivery/{delivery_id}/dashboard

        Buckets every order relative to one rider:
          - incoming: Pending, within the rider's service radius, not offered to
                      a different rider
          - active:   Assigned to this rider
          - recent:   Delivered by this rider (latest 10)
        """
        rider = request.user

        # The id stays in the URL so the route and the mobile app are unchanged,
        # but it is now checked rather than trusted. Walking the integer used to
        # return any rider's customer names, addresses and coordinates.
        if delivery_id != rider.id:
            raise PermissionDenied("You can only view your own dashboard.")

        orders = list(Order.objects.prefetch_related("items").order_by("-id"))

        # Fill in the distance for orders that have none stored. This assigns to
        # the in-memory instance and never calls `.save()`, so the request stays
        # a pure read — the same reason the FastAPI version built a separate
        # OrderOut instead of mutating the ORM object.
        for order in orders:
            if order.offered_distance_km is None:
                order.offered_distance_km = haversine_km(
                    rider.base_latitude,
                    rider.base_longitude,
                    order.customer_latitude,
                    order.customer_longitude,
                )

        active_order = next(
            (
                o
                for o in orders
                if o.delivery_boy_id == delivery_id and o.status == Order.ASSIGNED
            ),
            None,
        )

        recent_orders = [
            o
            for o in orders
            if o.delivery_boy_id == delivery_id and o.status == Order.DELIVERED
        ][:10]

        incoming_orders = [
            o
            for o in orders
            if o.status == Order.PENDING
            # not already claimed — a Pending order with a rider attached is in
            # an inconsistent state, and offering it would only produce a 409
            and o.delivery_boy_id is None
            # not already promised to a different rider
            and o.offered_to_delivery_boy_id in (None, delivery_id)
            # and close enough to be worth offering
            and (o.offered_distance_km or 0.0) <= rider.service_radius_km
        ]

        # Serialising *out* of a plain dict: DRF reads each declared field off
        # whatever object you give it, so a dict of already-filtered lists works
        # exactly as well as a model instance.
        payload = DeliveryDashboardSerializer(
            {
                "incoming_orders": incoming_orders,
                "active_order": active_order,
                "recent_orders": recent_orders,
            }
        )
        return Response(payload.data)
