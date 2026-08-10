"""Orders: manager listing/assignment, and rider accept/reject/status.

**Access is mixed in this module**, so unlike products.py nothing here inherits
`AdminAPIView`. Every view states its own `permission_classes` — `[IsAdmin]` for
the manager views, `[IsRider]` for the three the mobile app calls.

**The rider is taken from the token, never from the body.** These endpoints used
to read `delivery_boy_id` out of the request payload while requiring no
credentials at all, which meant any caller who could reach the host could claim,
reassign or complete any order by guessing an integer. `request.user` is now the
`User` row that `RiderJWTAuthentication` resolved, and the body's rider id is
gone. `AssignSerializer` still carries one because a *manager* legitimately
assigns work to someone other than themselves.
"""

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import Order, User
from api.permissions import IsAdmin, IsRider
from api.serializers import (
    AssignSerializer,
    OrderSerializer,
    StatusSerializer,
    SuccessSerializer,
)

# `prefetch_related("items")` fetches every order's line items in ONE extra
# query instead of one per order. It is the Django equivalent of the
# `lazy="selectin"` the SQLAlchemy relationship used, and without it the nested
# `items` on OrderSerializer would quietly become an N+1.
ORDERS = Order.objects.prefetch_related("items")


def get_order(order_id: int) -> Order:
    order = ORDERS.filter(pk=order_id).first()
    if order is None:
        raise NotFound("Order not found.")
    return order


def get_rider(rider_id: int) -> User:
    rider = User.objects.filter(pk=rider_id, role=User.DELIVERY).first()
    if rider is None:
        # 400, not 404: the id came from the request *body*, so it is a bad
        # payload rather than a missing resource at this URL.
        raise ValidationError("Delivery rider not found.")
    return rider


# --------------------------------------------------------------------------
# Manager views (admin token required)
# --------------------------------------------------------------------------
class OrderListView(APIView):
    permission_classes = [IsAdmin]

    @extend_schema(responses=OrderSerializer(many=True))
    def get(self, request):
        """GET /api/orders — newest first, each with its items nested."""
        return Response(OrderSerializer(ORDERS.order_by("-id"), many=True).data)


class OrderAssignView(APIView):
    permission_classes = [IsAdmin]

    @extend_schema(request=AssignSerializer, responses=OrderSerializer)
    def post(self, request, order_id: int):
        """POST /api/orders/{order_id}/assign — manager assigns a rider."""
        payload = AssignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        order = get_order(order_id)
        rider = get_rider(payload.validated_data["delivery_boy_id"])

        order.delivery_boy = rider
        order.status = Order.ASSIGNED
        order.save(update_fields=["delivery_boy", "status"])
        return Response(OrderSerializer(order).data)


# --------------------------------------------------------------------------
# Rider views (rider token required; the rider is `request.user`)
# --------------------------------------------------------------------------
class OrderStatusView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=StatusSerializer, responses=OrderSerializer)
    def patch(self, request, order_id: int):
        """PATCH /api/orders/{order_id}/status

        PATCH, not PUT, because it changes one field rather than replacing the
        order. The mobile app sends PATCH here and POST to accept/reject.
        """
        payload = StatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        order = get_order(order_id)

        # A rider may only move an order that is actually theirs. Without this,
        # a valid token for *any* rider could mark a colleague's order Delivered
        # or dump it back into the pool.
        if order.delivery_boy_id != request.user.id:
            raise PermissionDenied("This order is not assigned to you.")

        order.status = payload.validated_data["status"]
        fields = ["status"]

        # Returning an order to the pool must also release the rider. Otherwise
        # the order shows up in every rider's incoming feed (the feed filters on
        # status) while `accept` rejects all of them with 409 because it still
        # looks taken.
        if order.status == Order.PENDING:
            order.delivery_boy = None
            order.offered_to_delivery_boy = None
            fields += ["delivery_boy", "offered_to_delivery_boy"]

        order.save(update_fields=fields)
        return Response(OrderSerializer(order).data)


class OrderAcceptView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=None, responses=OrderSerializer)
    def post(self, request, order_id: int):
        """POST /api/orders/{order_id}/accept — rider claims a pending order.

        Takes no body: the claimant is the token holder.
        """
        rider = request.user

        # Claim and re-read inside one transaction, with the row locked, so two
        # riders tapping Accept at the same instant cannot both pass the checks
        # below before either writes. `select_for_update` is a no-op on SQLite
        # (which serialises writes anyway) but is what makes this correct once
        # DATABASE_URL points at Postgres.
        with transaction.atomic():
            order = Order.objects.select_for_update().filter(pk=order_id).first()
            if order is None:
                raise NotFound("Order not found.")

            # Only a Pending order can be claimed. Without this, a stale mobile
            # screen could POST /accept on an already-Delivered order and
            # resurrect it — pulling it out of the rider's recent list and back
            # into their active slot.
            if order.status != Order.PENDING:
                return Response(
                    {"detail": f"This order is no longer available (status: {order.status})."},
                    status=status.HTTP_409_CONFLICT,
                )

            # Two riders racing on the same order: whoever commits first wins,
            # the second gets a clear 409 instead of silently stealing it.
            if order.delivery_boy_id is not None and order.delivery_boy_id != rider.id:
                return Response(
                    {"detail": "This order has already been taken by another rider."},
                    status=status.HTTP_409_CONFLICT,
                )

            # An order offered to a *different* rider is not this rider's to
            # take, even while it is still Pending.
            if order.offered_to_delivery_boy_id not in (None, rider.id):
                return Response(
                    {"detail": "This order has been offered to another rider."},
                    status=status.HTTP_409_CONFLICT,
                )

            order.delivery_boy = rider
            order.status = Order.ASSIGNED
            order.offered_to_delivery_boy = None
            order.save(update_fields=["delivery_boy", "status", "offered_to_delivery_boy"])

        return Response(OrderSerializer(ORDERS.get(pk=order.pk)).data)


class OrderRejectView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=None, responses=SuccessSerializer)
    def post(self, request, order_id: int):
        """POST /api/orders/{order_id}/reject — rider declines an offered order.

        Clears the offer so the order returns to the pool for other riders.

        **Still largely a no-op**, and deliberately left that way: nothing in
        the system ever *sets* `offered_to_delivery_boy_id`, so in practice
        there is no offer to clear. Auth is now enforced and the rider comes
        from the token, but making reject meaningful needs a dispatch step that
        offers an order to one rider at a time — see the README's "Known gaps".
        """
        order = get_order(order_id)

        if order.offered_to_delivery_boy_id == request.user.id:
            order.offered_to_delivery_boy = None
            order.save(update_fields=["offered_to_delivery_boy"])

        return Response({"success": True})
