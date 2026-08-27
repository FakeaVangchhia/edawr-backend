"""Rider-facing endpoints for the mobile app.

The feed here is a **pull**: every available rider sees every nearby order that
is packed and unclaimed, minus the ones they personally declined, and whoever
taps Accept first gets it while the loser gets a clear 409.

**It is no longer the first thing that happens.** `api/dispatch.py` assigns a
Ready order to the nearest eligible rider synchronously, so in normal running an
order is Dispatched before it could ever reach this feed, and `incoming` is
empty. What is left here is the honest fallback: when automatic assignment finds
nobody — everyone off shift, out of range, or already carrying — the order stays
Ready and unassigned, and the first rider to come back on shift sees it.

Push *dispatch* was rejected here — offering an order to one rider at a time
needs a scheduler to expire an offer nobody answers, and there is no background
worker in this project. Assigning outright sidesteps that: no offer is pending,
so nothing has to expire it. See `api/dispatch.py` for the full reasoning.

Push *notifications* are a separate thing and they do exist: `api/push.py` buzzes
the handset when an order is assigned or lands in the feed, and
`RiderDeviceView` below is where the app registers for them. That answers the
cost `api/dispatch.py` names — a rider handed a drop while their phone is in a
pocket — without answering it with a queue. It changes nothing about how an
order is dispatched, and the fifteen-second poll remains the source of truth; a
notification is only a prompt to look.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from api import location as location_service
from api import push
from api.dispatch import haversine_km
from api.models import Order, OrderCustomerLocation, User
from api.permissions import IsAdmin, IsRider
from api.serializers import (
    DeliveryDashboardSerializer,
    RiderAvailabilitySerializer,
    RiderDeviceSerializer,
    RiderLocationReportSerializer,
    RiderLocationSerializer,
    UserSerializer,
)

# How many completed orders the app shows in its history tab. The rider scrolls
# this on a phone; a full history would be a slow query for a list nobody reads
# to the end.
RECENT_LIMIT = 10

ORDERS = Order.objects.prefetch_related("items").select_related("delivery_boy")


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


class RiderDeviceView(APIView):
    """The rider app's push-notification registration. See `api/push.py`.

    POST registers this handset, DELETE forgets it. Both take the token in the
    body and the rider from their bearer token, so — as with `accept` and
    `status` — there is no rider id to walk and nothing to spoof by editing a
    request.

    **Register on every launch, not once.** Expo rotates a push token whenever
    the app is reinstalled, restored to a new phone, or updated across certain
    native boundaries, and it never tells the server it did. The app re-POSTs on
    each sign-in and each launch; `push.register_device` upserts on the token,
    so the repeat is free and the row cannot drift out of date.

    **DELETE is what sign-out is for.** A rider handing back a shared handset at
    shift change would otherwise keep receiving the next rider's drops on it —
    the notification is delivered by Expo, which knows nothing about our tokens
    expiring. It is idempotent: forgetting a phone that is already forgotten is
    a 204, because the caller cannot know which it was and the outcome they
    wanted is the same either way.
    """

    permission_classes = [IsRider]

    @extend_schema(request=RiderDeviceSerializer, responses={204: None})
    def post(self, request):
        payload = RiderDeviceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        push.register_device(
            request.user,
            payload.validated_data["expo_token"],
            payload.validated_data.get("platform", ""),
        )
        # 204 rather than the row: the app has nothing to do with the id, and
        # echoing a credential-shaped value back is a habit worth not forming.
        return Response(status=http.HTTP_204_NO_CONTENT)

    @extend_schema(request=RiderDeviceSerializer, responses={204: None})
    def delete(self, request):
        payload = RiderDeviceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        push.forget_device(request.user, payload.validated_data["expo_token"])
        return Response(status=http.HTTP_204_NO_CONTENT)


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

        # Where the customer says they are waiting, for the order this rider is
        # actually carrying and for no other. Scoped to `active_order` — which
        # is already filtered to `delivery_boy_id=rider.id` — so a rider cannot
        # reach the live position of somebody else's customer.
        #
        # Carried on the dashboard rather than fetched separately so the app's
        # existing fifteen-second poll picks it up for free. A second request
        # every fifteen seconds is a real cost on Aizawl mobile data, and this
        # is a handful of bytes on a response the app already makes.
        customer_location = None
        if active_order is not None:
            customer_location = OrderCustomerLocation.objects.filter(
                order_id=active_order.id
            ).first()

        payload = DeliveryDashboardSerializer(
            {
                "incoming_orders": incoming_orders,
                "active_order": active_order,
                "recent_orders": recent_orders,
                "is_available": rider.is_available,
                "customer_location": customer_location,
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
            # The rejection filter: this is what makes the Reject button real.
            .exclude(rejections__rider_id=rider.id)
            .order_by("-id")
        )

        # Distance is computed in Python because haversine is not portable SQL,
        # and it is assigned to the in-memory instance without ever calling
        # save() — this request stays a pure read.
        #
        # An order with no coordinates is offered to everyone with a distance of
        # None. It is the same rule `dispatch._rank` applies and for the same
        # reason: position is optional at checkout, and the alternative the
        # columns used to default to made every such order read as 0.00 km away
        # — a number the rider app displayed as confident fact.
        in_range = []
        for order in candidates:
            if order.customer_latitude is None or order.customer_longitude is None:
                order.offered_distance_km = None
                in_range.append(order)
                continue

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
        # always the right one to take next. Unknown distances sort last rather
        # than first, so a drop the rider knows is nearby is always on top.
        in_range.sort(
            key=lambda order: (
                order.offered_distance_km is None,
                order.offered_distance_km or 0.0,
            )
        )
        return in_range


class RiderLocationReportView(APIView):
    """POST /api/delivery/location — one position fix from the rider's handset.

    **No rider id, in the path or the body.** The rider is the token, exactly as
    for accept, reject and status. There is nothing here to check ownership of
    because there is nothing to spoof: a valid rider token can only ever move
    its own marker.

    Its own throttle scope on purpose. This is the highest-frequency authenticated
    endpoint in the API, and without a separate bucket a location loop stuck on a
    retry would spend the rider's whole `staff` allowance — after which the next
    thing they could not do is accept an order. Telemetry must not be able to
    starve the work.

    Answers **200** with what was stored, not 204. The handset needs two facts
    back that it cannot know on its own: the server's clock, so a device whose
    own is wrong can notice, and which order the fix was attributed to, so the
    app can stop reporting when it turns out it is no longer carrying anything.
    """

    permission_classes = [IsRider]
    throttle_scope = "rider_location"

    @extend_schema(request=RiderLocationReportSerializer, responses=None)
    def post(self, request):
        payload = RiderLocationReportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        stored, order = location_service.record_rider_position(
            request.user,
            latitude=data["latitude"],
            longitude=data["longitude"],
            accuracy_m=data.get("accuracy_m"),
            speed_kmh=data.get("speed_kmh"),
            heading=data.get("heading"),
            recorded_at=data.get("recorded_at"),
        )

        return Response(
            {
                "received_at": stored.received_at,
                # Null when the rider is between drops. The app reads this as
                # "stop reporting": there is no delivery to track, and a phone
                # that keeps sending is spending battery to record where an
                # off-duty person went.
                "order_id": order.id if order is not None else None,
            }
        )


class RiderLocationListView(APIView):
    """GET /api/delivery/locations — where every active rider is, for the console.

    Staff-facing, so it carries rider identity and fix accuracy — a manager
    deciding who to send needs to know that one of those markers is a
    two-kilometre guess. It still carries no `base_latitude`/`base_longitude`:
    that is the rider's home address and it belongs to the staff editor.

    Stale positions are returned and flagged rather than dropped. See
    `location.console_roster` — a manager can act on "last seen 4 minutes ago",
    and cannot act on a rider who has silently vanished from the map.
    """

    permission_classes = [IsAdmin]

    @extend_schema(responses=RiderLocationSerializer(many=True))
    def get(self, request):
        roster = location_service.console_roster()
        return Response(RiderLocationSerializer(roster, many=True).data)
