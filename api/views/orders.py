"""Orders: the manager's queue and the rider's job list.

**Access is mixed in this module**, so unlike products.py nothing here inherits
`AdminAPIView`. Every view states its own `permission_classes`, and the bare
ones are meant to stand out.

**The rider is taken from the token, never from the body.** These endpoints used
to read `delivery_boy_id` out of the request payload while requiring no
credentials at all, which meant any caller who could reach the host could claim,
reassign or complete any order by guessing an integer. `request.user` is now the
`User` row that `RiderJWTAuthentication` resolved. `AssignSerializer` still
carries a rider id because a *manager* legitimately assigns work to someone else.

**Nothing here assigns `order.status` directly.** Every change goes through
`Order.advance_status`, which refuses illegal moves, or through
`checkout.cancel_order`, which additionally puts the stock back. A view that set
the field itself could mark a cancelled order Delivered, and the audit
timestamps would silently disagree with the status.
"""

from __future__ import annotations

import logging

from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status as http
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from api.checkout import cancel_order
from api.models import Order, OrderRejection, User
from api.paging import read_page
from api.permissions import IsAdmin, IsAdminOrRider, IsRider
from api.serializers import (
    AssignSerializer,
    OrderSerializer,
    StatusSerializer,
    SuccessSerializer,
)

logger = logging.getLogger(__name__)

# `prefetch_related("items")` fetches every order's line items in ONE extra
# query instead of one per order; `select_related` does the same for the rider,
# which OrderSerializer nests. Without both, listing 50 orders is 101 queries.
ORDERS = Order.objects.prefetch_related("items").select_related("delivery_boy")

# What each kind of caller is allowed to ask for. The order's own state machine
# still has the final say — this is about authority, not sequence. A rider may
# never cancel (that decision, and the refund conversation behind it, belongs to
# the store), and a manager may not mark an order Dispatched, because dispatch
# means a specific rider physically took it.
ADMIN_TARGETS = frozenset({Order.PACKING, Order.READY, Order.DELIVERED, Order.CANCELLED})
RIDER_TARGETS = frozenset({Order.DELIVERED, Order.READY})


def get_order(order_id: int) -> Order:
    order = ORDERS.filter(pk=order_id).first()
    if order is None:
        raise NotFound("Order not found.")
    return order


def get_rider(rider_id: int) -> User:
    rider = User.objects.filter(pk=rider_id, role=User.DELIVERY, is_active=True).first()
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

    @extend_schema(
        parameters=[
            OpenApiParameter("status", str, description="Filter by exact status."),
            OpenApiParameter("open", bool, description="Only orders not yet delivered or cancelled."),
            OpenApiParameter(
                "stalled",
                bool,
                description="Ready orders every available rider has declined, or that are past their promised time.",
            ),
            OpenApiParameter("limit", int),
            OpenApiParameter("offset", int),
        ],
        responses=OrderSerializer(many=True),
    )
    def get(self, request):
        """GET /api/orders — newest first, each with its items nested."""
        orders = ORDERS.order_by("-id")

        wanted = (request.query_params.get("status") or "").strip()
        if wanted:
            orders = orders.filter(status=wanted)

        if _flag(request, "open"):
            orders = orders.exclude(status__in=Order.TERMINAL)

        if _flag(request, "stalled"):
            return Response(OrderSerializer(self._stalled(orders), many=True).data)

        limit, offset = read_page(request, default=50, maximum=200)
        return Response(OrderSerializer(orders[offset : offset + limit], many=True).data)

    @staticmethod
    def _stalled(orders) -> list[Order]:
        """Ready orders that no rider is going to pick up on their own.

        Rejections are per-rider and dispatch is a pull, so an order every
        available rider has declined simply stops appearing in any feed. Without
        this view it would sit there indefinitely with nothing to show for it.
        An order past its promised time counts too — nobody has taken it and the
        customer is already waiting.
        """
        available = set(
            User.objects.filter(
                role=User.DELIVERY, is_active=True, is_available=True
            ).values_list("id", flat=True)
        )

        ready = list(orders.filter(status=Order.READY).prefetch_related("rejections"))

        stalled = []
        for order in ready:
            rejected_by = {rejection.rider_id for rejection in order.rejections.all()}
            if not available or available <= rejected_by or order.is_late:
                stalled.append(order)
        return stalled


class OrderAssignView(APIView):
    permission_classes = [IsAdmin]

    @extend_schema(request=AssignSerializer, responses=OrderSerializer)
    def post(self, request, order_id: int):
        """POST /api/orders/{order_id}/assign — manager hands an order to a rider.

        The manual override for when dispatch stalls: it bypasses the pull feed
        and the rider's own rejection, because a manager who has just spoken to
        someone knows more than the queue does.
        """
        payload = AssignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        rider = get_rider(payload.validated_data["delivery_boy_id"])

        with transaction.atomic():
            order = Order.objects.select_for_update().filter(pk=order_id).first()
            if order is None:
                raise NotFound("Order not found.")

            if order.status in Order.TERMINAL:
                return Response(
                    {"detail": f"This order is already {order.get_status_display().lower()}."},
                    status=http.HTTP_409_CONFLICT,
                )

            # An order still being picked cannot be handed over yet, so bring it
            # to Ready first. Both moves go through the state machine, so an
            # order in a state that cannot reach Ready is refused rather than
            # forced.
            changed: list[str] = []
            if order.status in (Order.PLACED, Order.PACKING):
                try:
                    if order.status == Order.PLACED:
                        changed += order.advance_status(Order.PACKING)
                        order.save(update_fields=changed)
                        changed = []
                    changed += order.advance_status(Order.READY)
                except ValueError as exc:
                    return Response({"detail": str(exc)}, status=http.HTTP_409_CONFLICT)

            try:
                changed += order.advance_status(Order.DISPATCHED)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=http.HTTP_409_CONFLICT)

            order.delivery_boy = rider
            order.offered_to_delivery_boy = None
            changed += ["delivery_boy", "offered_to_delivery_boy"]
            order.save(update_fields=list(dict.fromkeys(changed)))

        logger.info(
            "order assigned by manager",
            extra={"order_id": order_id, "rider_id": rider.id},
        )
        return Response(OrderSerializer(ORDERS.get(pk=order_id)).data)


# --------------------------------------------------------------------------
# Shared: status changes
# --------------------------------------------------------------------------
class OrderStatusView(APIView):
    """PATCH /api/orders/{order_id}/status

    Called by both the admin console and the rider app. What each may request is
    `ADMIN_TARGETS` / `RIDER_TARGETS`; whether the move is legal *from here* is
    `Order.TRANSITIONS`. Both checks apply.
    """

    permission_classes = [IsAdminOrRider]

    @extend_schema(request=StatusSerializer, responses=OrderSerializer)
    def patch(self, request, order_id: int):
        payload = StatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        target = payload.validated_data["status"]

        is_rider = isinstance(request.user, User)
        allowed = RIDER_TARGETS if is_rider else ADMIN_TARGETS
        if target not in allowed:
            raise PermissionDenied(f"You cannot move an order to {target}.")

        # Cancelling is not a plain status change: it has to put the stock back,
        # which means re-reading the order under a lock and rewriting products.
        # That whole operation lives in checkout.cancel_order.
        if target == Order.CANCELLED:
            cancel_order(get_order(order_id), "Cancelled by store")
            return Response(OrderSerializer(ORDERS.get(pk=order_id)).data)

        with transaction.atomic():
            order = Order.objects.select_for_update().filter(pk=order_id).first()
            if order is None:
                raise NotFound("Order not found.")

            # A rider may only move an order that is actually theirs. Without
            # this, a valid token for *any* rider could mark a colleague's order
            # Delivered or dump it back into the pool.
            if is_rider and order.delivery_boy_id != request.user.id:
                raise PermissionDenied("This order is not assigned to you.")

            try:
                changed = order.advance_status(target)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=http.HTTP_409_CONFLICT)

            # Handing an order back to the pool must also release the rider,
            # or it shows up as unclaimed while still looking taken — and every
            # accept attempt then fails with a 409 nobody can explain.
            if target == Order.READY:
                order.delivery_boy = None
                order.offered_to_delivery_boy = None
                changed += ["delivery_boy", "offered_to_delivery_boy"]

            order.save(update_fields=list(dict.fromkeys(changed)))

        logger.info(
            "order status changed",
            extra={
                "order_id": order_id,
                "to": target,
                "by": "rider" if is_rider else "admin",
            },
        )
        return Response(OrderSerializer(ORDERS.get(pk=order_id)).data)


# --------------------------------------------------------------------------
# Rider views (rider token required; the rider is `request.user`)
# --------------------------------------------------------------------------
class OrderAcceptView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=None, responses=OrderSerializer)
    def post(self, request, order_id: int):
        """POST /api/orders/{order_id}/accept — rider claims a packed order.

        Takes no body: the claimant is the token holder.
        """
        rider = request.user

        # Claim and re-read inside one transaction with the row locked, so two
        # riders tapping Accept at the same instant cannot both pass the checks
        # below before either writes. `select_for_update` is a no-op on SQLite
        # (which serialises writes anyway) but is what makes this correct once
        # DATABASE_URL points at Postgres.
        with transaction.atomic():
            order = Order.objects.select_for_update().filter(pk=order_id).first()
            if order is None:
                raise NotFound("Order not found.")

            # Only a Ready order can be claimed — one still being picked has
            # nothing to hand over. The state machine enforces the sequence; this
            # returns the friendlier message first.
            if order.status != Order.READY:
                return Response(
                    {"detail": f"This order is no longer available ({order.get_status_display().lower()})."},
                    status=http.HTTP_409_CONFLICT,
                )

            # Two riders racing on the same order: whoever commits first wins,
            # the second gets a clear 409 instead of silently stealing it.
            if order.delivery_boy_id is not None and order.delivery_boy_id != rider.id:
                return Response(
                    {"detail": "This order has already been taken by another rider."},
                    status=http.HTTP_409_CONFLICT,
                )

            # An order a manager offered to a *specific* rider is not anyone
            # else's to take.
            if order.offered_to_delivery_boy_id not in (None, rider.id):
                return Response(
                    {"detail": "This order has been offered to another rider."},
                    status=http.HTTP_409_CONFLICT,
                )

            try:
                changed = order.advance_status(Order.DISPATCHED)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=http.HTTP_409_CONFLICT)

            order.delivery_boy = rider
            order.offered_to_delivery_boy = None
            changed += ["delivery_boy", "offered_to_delivery_boy"]
            order.save(update_fields=list(dict.fromkeys(changed)))

        logger.info("order accepted", extra={"order_id": order_id, "rider_id": rider.id})
        return Response(OrderSerializer(ORDERS.get(pk=order_id)).data)


class OrderRejectView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=None, responses=SuccessSerializer)
    def post(self, request, order_id: int):
        """POST /api/orders/{order_id}/reject — rider declines this order.

        **This used to do nothing.** It cleared `offered_to_delivery_boy_id`,
        but nothing in the system ever set that column, so the order reappeared
        in the rider's feed on the next refresh and the button was decoration.

        It now records the decline in `order_rejections`, and the rider's feed
        excludes anything they are listed against. The rider stops seeing it;
        every other rider still does. `get_or_create` makes a double tap
        idempotent rather than an integrity error.

        An order declined by *everyone* stops appearing anywhere, which is why
        `GET /api/orders?stalled=true` exists for the manager.
        """
        order = get_order(order_id)

        if order.status in Order.TERMINAL:
            return Response(
                {"detail": f"This order is already {order.get_status_display().lower()}."},
                status=http.HTTP_409_CONFLICT,
            )

        if order.delivery_boy_id == request.user.id:
            # Declining an order you already accepted is a different action:
            # it has to release you from it, which is what the status endpoint
            # does. Refusing here keeps the two from quietly disagreeing.
            return Response(
                {
                    "detail": (
                        "You have already accepted this order. Move it back to "
                        "Ready to hand it over."
                    )
                },
                status=http.HTTP_409_CONFLICT,
            )

        OrderRejection.objects.get_or_create(order=order, rider=request.user)

        if order.offered_to_delivery_boy_id == request.user.id:
            order.offered_to_delivery_boy = None
            order.save(update_fields=["offered_to_delivery_boy"])

        logger.info(
            "order rejected", extra={"order_id": order_id, "rider_id": request.user.id}
        )
        return Response({"success": True})


def _flag(request, name: str) -> bool:
    """Read a boolean query parameter.

    `?stalled=false` must be False, and `bool("false")` is True — the classic
    way to ship a filter that is always on.
    """
    return (request.query_params.get(name) or "").strip().lower() in {"1", "true", "yes"}
