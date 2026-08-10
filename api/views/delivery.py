"""Rider-facing endpoints for the mobile app.

Dispatch here is a **pull**, not a push. There is no scheduler handing orders to
one rider at a time and timing them out; instead every available rider sees
every nearby order that is packed and unclaimed, minus the ones they personally
declined. Whoever taps Accept first gets it, and the loser gets a clear 409.

That design was chosen over push dispatch because push needs a background worker
and a timeout policy to be correct — an offer nobody answers has to expire, and
something has to be running to expire it. A pull feed needs neither and degrades
honestly: the worst case is an order nobody takes, which `GET /api/orders?
stalled=true` shows the manager directly.
"""

from __future__ import annotations

import math

from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import Order, User
from api.permissions import IsAdmin, IsRider
from api.serializers import (
    DeliveryDashboardSerializer,
    RiderAvailabilitySerializer,
    UserSerializer,
)

EARTH_RADIUS_KM = 6371.0

# How many completed orders the app shows in its history tab. The rider scrolls
# this on a phone; a full history would be a slow query for a list nobody reads
# to the end.
RECENT_LIMIT = 10

ORDERS = Order.objects.prefetch_related("items").select_related("delivery_boy")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres.

    Straight-line distance, not travel distance — Aizawl is built on ridges and
    the road distance can be several times this. It is used only to decide
    whether an order is *plausibly* in a rider's area, which is a job it does
    well enough; do not present it to anyone as an ETA.
    """
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class RiderListView(APIView):
    """GET /api/delivery/riders — the rider roster, for managers.

    **Not public.** It used to back the mobile login screen's "pick your
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


class RiderAvailabilityView(APIView):
    """PATCH /api/delivery/availability — the rider's own on/off switch.

    Distinct from `is_active`, which is the manager's switch. "I am on a break"
    and "this person no longer works here" must not be the same flag, or a rider
    could re-enable their own dismissed account by toggling a button.
    """

    permission_classes = [IsRider]

    @extend_schema(request=RiderAvailabilitySerializer, responses=UserSerializer)
    def patch(self, request):
        payload = RiderAvailabilitySerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        rider = request.user
        rider.is_available = payload.validated_data["is_available"]
        rider.save(update_fields=["is_available"])
        return Response(UserSerializer(rider).data)


class RiderDashboardView(APIView):
    """A rider's own feed. The `delivery_id` in the path must be the caller."""

    permission_classes = [IsRider]

    @extend_schema(responses=DeliveryDashboardSerializer)
    def get(self, request, delivery_id: int):
        """GET /api/delivery/{delivery_id}/dashboard

        Three buckets:
          - incoming: Ready, unclaimed, in range, not declined by this rider
          - active:   Dispatched to this rider
          - recent:   Delivered by this rider (latest 10)
        """
        rider = request.user

        # The id stays in the URL so the route and the mobile app are unchanged,
        # but it is checked rather than trusted. Walking the integer used to
        # return any rider's customer names, addresses and coordinates.
        if delivery_id != rider.id:
            raise PermissionDenied("You can only view your own dashboard.")

        active_order = (
            ORDERS.filter(delivery_boy_id=rider.id, status=Order.DISPATCHED)
            .order_by("-id")
            .first()
        )

        recent_orders = list(
            ORDERS.filter(delivery_boy_id=rider.id, status=Order.DELIVERED).order_by(
                "-delivered_at", "-id"
            )[:RECENT_LIMIT]
        )

        incoming_orders = self._incoming(rider)

        payload = DeliveryDashboardSerializer(
            {
                "incoming_orders": incoming_orders,
                "active_order": active_order,
                "recent_orders": recent_orders,
                "is_available": rider.is_available,
            }
        )
        return Response(payload.data)

    @staticmethod
    def _incoming(rider: User) -> list[Order]:
        """Packed orders this rider could take, nearest first.

        A rider who has marked themselves unavailable, or who already has an
        order in hand, is offered nothing — a 10-minute delivery promise does
        not survive stacking two drops on one rider.
        """
        if not rider.is_available:
            return []

        already_carrying = Order.objects.filter(
            delivery_boy_id=rider.id, status=Order.DISPATCHED
        ).exists()
        if already_carrying:
            return []

        candidates = (
            ORDERS.filter(status=Order.READY, delivery_boy__isnull=True)
            # Not promised to somebody else by a manager.
            .filter(offered_to_delivery_boy__isnull=True)
            # The rejection filter: this is what makes the Reject button real.
            .exclude(rejections__rider_id=rider.id)
            .order_by("-id")
        )

        # Distance is computed in Python because haversine is not portable SQL,
        # and it is assigned to the in-memory instance without ever calling
        # save() — this request stays a pure read.
        in_range = []
        for order in candidates:
            distance = haversine_km(
                rider.base_latitude,
                rider.base_longitude,
                order.customer_latitude,
                order.customer_longitude,
            )
            order.offered_distance_km = round(distance, 2)
            if distance <= rider.service_radius_km:
                in_range.append(order)

        # Nearest first: on a 10-minute promise the closest drop is almost
        # always the right one to take next.
        in_range.sort(key=lambda order: order.offered_distance_km)
        return in_range
