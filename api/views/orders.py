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

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status as http
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from api import audit, dispatch, push
from api.checkout import cancel_order, restock_failed_order
from api.models import AuditLog, Order, OrderRejection, User
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
ADMIN_TARGETS = frozenset(
    {Order.PACKING, Order.READY, Order.DELIVERED, Order.CANCELLED, Order.FAILED}
)
# `Failed` is a rider target because the rider is the one standing at the door
# when it happens. Until it existed, a customer who refused the bag, an address
# nobody answered and a stolen bike all had the same only button — "Mark
# delivered" — so the goods were recorded as sold and paid for and the stock
# never came back.
RIDER_TARGETS = frozenset({Order.DELIVERED, Order.READY, Order.FAILED})

# The vocabulary `?status=` will accept, in declaration order so the error
# message reads like the state machine rather than like a set.
VALID_STATUSES = [value for value, _ in Order.STATUS_CHOICES]


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
            # Validated rather than passed through. An unrecognised value used
            # to filter to nothing and return `[]` with `X-Total-Count: 0`,
            # which is indistinguishable from "there are no delivered orders" —
            # so `?status=Delivred` looked like an empty shop rather than a typo.
            if wanted not in VALID_STATUSES:
                raise ValidationError(
                    f"Unknown status '{wanted}'. Expected one of: "
                    + ", ".join(VALID_STATUSES)
                    + "."
                )
            orders = orders.filter(status=wanted)

        if _flag(request, "open"):
            orders = orders.exclude(status__in=Order.TERMINAL)

        # `?rider=` and the date range are what turn this endpoint into an order
        # *history*. Until they existed the console could only ever ask for open
        # orders, so a customer ringing about yesterday could not be looked up at
        # all — the order was in the database and unreachable from the UI.
        rider = (request.query_params.get("rider") or "").strip()
        if rider.isdigit():
            orders = orders.filter(delivery_boy_id=int(rider))

        query = (request.query_params.get("q") or "").strip()
        if query:
            match = (
                Q(customer_name__icontains=query)
                | Q(customer_phone__icontains=query)
                | Q(customer_address__icontains=query)
            )
            # A bare number is almost always someone reading an order id off a
            # slip, so match that too rather than making them use a second box.
            if query.lstrip("#").isdigit():
                match |= Q(pk=int(query.lstrip("#")))
            orders = orders.filter(match)

        tz = ZoneInfo(settings.STORE_TIMEZONE)
        from_date = _read_date(request, "from")
        if from_date:
            orders = orders.filter(
                created_at__gte=datetime.combine(from_date, time.min, tzinfo=tz)
            )
        to_date = _read_date(request, "to")
        if to_date:
            # Half-open against the midnight *after* to_date, so an inclusive
            # range does not silently drop everything ordered on the last day.
            orders = orders.filter(
                created_at__lt=datetime.combine(
                    to_date + timedelta(days=1), time.min, tzinfo=tz
                )
            )

        limit, offset = read_page(request, default=50, maximum=200)

        if _flag(request, "stalled"):
            # Paged like every other branch. It used to return here, before the
            # paging below, so this one query parameter combination answered
            # with an unbounded list and no `X-Total-Count` — and the console's
            # paginator, which reads that header, silently reported the page
            # length as the total.
            #
            # Stalled-ness cannot be expressed in SQL (it depends on rider
            # positions), so the slice happens in Python after the fact. The
            # set is small by construction: only Ready orders reach it.
            stalled = self._stalled(orders)
            response = Response(
                OrderSerializer(stalled[offset : offset + limit], many=True).data
            )
            response["X-Total-Count"] = str(len(stalled))
            return response

        total = orders.count()
        response = Response(
            OrderSerializer(orders[offset : offset + limit], many=True).data
        )
        response["X-Total-Count"] = str(total)
        return response

    @staticmethod
    def _stalled(orders) -> list[Order]:
        """Ready orders that nobody is going to pick up without a manager.

        An order still Ready has not been assigned, which under automatic
        dispatch means `api/dispatch.py` found no eligible rider. The reasons
        divide in two, and only one of them is a problem:

          - **Waiting.** Every rider in range is mid-delivery. It resolves
            itself the moment one of them drops off, and listing it here would
            make the stalled queue mostly noise.
          - **Stuck.** Nobody on shift is within range, or everyone in range has
            declined it. Nothing will change on its own.

        `dispatch.reachable_riders` draws exactly that line — it applies the
        dispatch rule but ignores who is carrying — so the two definitions
        cannot drift apart. Before it, this method never considered distance at
        all: an order nobody could reach stayed invisible until `is_late` tripped
        it, which is *after* the customer has waited out the whole promise.

        An order past its promised time still counts regardless. Whatever the
        reason, the customer is already waiting.
        """
        ready = list(orders.filter(status=Order.READY).prefetch_related("rejections"))

        return [
            order
            for order in ready
            if order.is_late or not dispatch.reachable_riders(order)
        ]


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
            changed += ["delivery_boy"]
            order.save(update_fields=list(dict.fromkeys(changed)))

            # The same buzz automatic assignment sends, because to the rider it
            # is the same event: an order is theirs and they did not ask for it.
            # Inside the block and after the save — `api/push.py` defers the
            # send until this transaction commits.
            push.notify_assigned(order, rider)

        logger.info(
            "order assigned by manager",
            extra={"order_id": order_id, "rider_id": rider.id},
        )
        audit.record(
            request, AuditLog.ASSIGN, "order", order_id,
            f"Assigned order #{order_id} to {rider.name}",
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
            reason = (payload.validated_data.get("reason") or "").strip()
            cancel_order(get_order(order_id), reason or "Cancelled by store")
            audit.record(
                request, AuditLog.CANCEL, "order", order_id,
                f"Cancelled order #{order_id}"
                + (f" - {reason}" if reason else " (no reason given)"),
            )
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

            previous_status = order.status
            try:
                changed = order.advance_status(target)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=http.HTTP_409_CONFLICT)

            # A failed delivery without a recorded reason is an order nobody can
            # later explain to the customer who rings about it. The reason is
            # required for exactly that: this is the one transition whose whole
            # value is the sentence attached to it.
            if target == Order.FAILED:
                reason = (payload.validated_data.get("reason") or "").strip()
                if not reason:
                    raise ValidationError(
                        "Say what went wrong — this is what the store will read "
                        "when the customer calls."
                    )
                order.cancellation_reason = reason
                changed.append("cancellation_reason")

            # `advance_status` has already stamped `paid_at` and set
            # `amount_collected` to the full total, so a collection is recorded
            # whichever route reached Delivered. What is left to do here is the
            # part only the request knows: who took the money, and whether the
            # customer actually handed over all of it.
            # Keyed on `paid_at` having just been stamped rather than on the
            # target alone, so a repeat PATCH of an already-Delivered order —
            # which `advance_status` answers with an empty change list — cannot
            # rewrite a collection that was recorded at the door an hour ago.
            if target == Order.DELIVERED and "paid_at" in changed:
                if is_rider:
                    order.collected_by = request.user
                    changed.append("collected_by")

                stated = payload.validated_data.get("amount_collected")
                if stated is not None:
                    # Capped at the order's value, because anything above it is
                    # not a collection — it is change the rider owes back, and
                    # recording it as revenue would make the till reconcile
                    # against money the store never kept.
                    if stated > order.grand_total:
                        raise ValidationError(
                            f"You cannot collect more than the order total of "
                            f"₹{order.grand_total}."
                        )
                    order.amount_collected = stated
                    changed.append("amount_collected")

            # Handing an order back to the pool must also release the rider,
            # or it shows up as unclaimed while still looking taken — and every
            # accept attempt then fails with a 409 nobody can explain.
            handed_back_by = None
            if target == Order.READY:
                if is_rider:
                    # Remember who let go of it *before* clearing the column.
                    # Automatic assignment picks the nearest rider, which is very
                    # often the one who just handed it back, and the order would
                    # ping-pong between them until somebody opened the console.
                    handed_back_by = request.user
                order.delivery_boy = None
                changed += ["delivery_boy"]

            order.save(update_fields=list(dict.fromkeys(changed)))

            # A short collection is the one thing here that is about money
            # rather than about logistics, so it goes in the audit trail with
            # the figures beside it — that log entry is what a manager reads
            # when the till does not balance at the end of the shift.
            summary = f"Moved order #{order_id} to {target}"
            changes = {"status": [previous_status, target]}
            if "amount_collected" in changed and order.amount_collected < order.grand_total:
                summary += (
                    f" - collected ₹{order.amount_collected} of ₹{order.grand_total}"
                )
                changes["amount_collected"] = [
                    str(order.grand_total),
                    str(order.amount_collected),
                ]

            audit.record(
                request, AuditLog.STATUS, "order", order_id, summary, changes
            )

            # Dispatch, in the same transaction as the move that made the order
            # collectable. Finding nobody is not an error: the order stays Ready
            # and unassigned, which is what `?stalled=true` surfaces and what the
            # rider feed still serves. See api/dispatch.py.
            if target == Order.READY:
                if handed_back_by is not None:
                    dispatch.decline_for(order, handed_back_by)
                dispatch.auto_assign(order, request=request)

            # And tell the customer, if they have an account with a phone
            # registered. Inside the transaction like the dispatch call above,
            # because `notify_customer_status` defers its own send to
            # `on_commit` - a rollback after this point must not buzz somebody
            # about a status the database never reached. It cannot raise; see
            # the contract in api/push.py.
            push.notify_customer_status(order)

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

            try:
                changed = order.advance_status(Order.DISPATCHED)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=http.HTTP_409_CONFLICT)

            order.delivery_boy = rider
            changed += ["delivery_boy"]
            order.save(update_fields=list(dict.fromkeys(changed)))

        logger.info("order accepted", extra={"order_id": order_id, "rider_id": rider.id})
        return Response(OrderSerializer(ORDERS.get(pk=order_id)).data)


class OrderRestockView(APIView):
    """POST /api/orders/{order_id}/restock — the goods came back.

    The second half of the failed-delivery path, and separate from marking the
    order Failed on purpose. When the rider reports a refusal the bag is on a
    bike somewhere; restocking then would list units the store cannot pick.
    A manager presses this when the goods are physically on the shelf again.

    `checkout.restock_failed_order` makes it idempotent through `restocked_at`,
    so two managers clicking at once cannot double the inventory.
    """

    permission_classes = [IsAdmin]

    @extend_schema(request=None, responses=OrderSerializer)
    def post(self, request, order_id: int):
        order = restock_failed_order(get_order(order_id))
        audit.record(
            request, AuditLog.UPDATE, "order", order_id,
            f"Returned the stock from failed order #{order_id}",
        )
        return Response(OrderSerializer(ORDERS.get(pk=order_id)).data)


class OrderRejectView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=None, responses=SuccessSerializer)
    def post(self, request, order_id: int):
        """POST /api/orders/{order_id}/reject — rider declines this order.

        **This used to do nothing.** It cleared `offered_to_delivery_boy_id`,
        a column nothing ever set, so the order reappeared in the rider's feed
        on the next refresh and the button was decoration. That column is now
        gone (migration 0007); this table is what replaced it.

        It now records the decline in `order_rejections`, and the rider's feed
        excludes anything they are listed against. The rider stops seeing it;
        every other rider still does. `get_or_create` makes a double tap
        idempotent rather than an integrity error.

        An order declined by *everyone* stops appearing anywhere, which is why
        `GET /api/orders?stalled=true` exists for the manager.

        **A rider may only decline an order they were actually shown.** Order
        ids are sequential, and the only checks here used to be "not terminal"
        and "not already mine" — so one rider token could walk the id space and
        pre-decline every order in the store, including ones not yet packed.
        Each row is permanent and each one removes that rider from
        `reachable_riders`, so the orders would later reach Ready with nobody
        eligible and go straight to the stalled queue, looking like a staffing
        problem rather than an attack. The two checks below are what make the
        button mean "not this one, thanks" rather than "none, ever".
        """
        order = get_order(order_id)

        # Idempotency comes first, and it has to. Everything below narrows who
        # may decline — including "you must still be a candidate", which a rider
        # stops being the instant their first decline is recorded. Checking
        # eligibility before this would turn the second tap of a double tap on
        # flaky mobile data into a 409, which is the exact failure the
        # get_or_create below was chosen to avoid. Answering success for a
        # decline already on record is also simply true.
        if OrderRejection.objects.filter(order=order, rider=request.user).exists():
            return Response({"success": True})

        if order.status in Order.TERMINAL:
            return Response(
                {"detail": f"This order is already {order.get_status_display().lower()}."},
                status=http.HTTP_409_CONFLICT,
            )

        # Only a bagged order is on offer. Placed and Packing orders are not in
        # anyone's feed yet, and Dispatched belongs to whoever took it.
        if order.status != Order.READY:
            return Response(
                {"detail": "This order is not available to accept or decline."},
                status=http.HTTP_409_CONFLICT,
            )

        # ...and only to riders it would actually be offered to. Same rule the
        # feed is built from (`RiderDashboardView._incoming` and
        # `dispatch.reachable_riders`), so what a rider can decline is exactly
        # what a rider can see. Note this runs *before* the rejection row is
        # written, so a rider out of range cannot put themselves on record
        # against an order they were never a candidate for.
        if request.user.id not in {
            rider.id for _, rider in dispatch.reachable_riders(order)
        }:
            return Response(
                {"detail": "This order was not offered to you."},
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

        logger.info(
            "order rejected", extra={"order_id": order_id, "rider_id": request.user.id}
        )
        return Response({"success": True})


def _read_date(request, name: str):
    """Parse `?from=YYYY-MM-DD`, or None. Garbage is ignored, never a 500."""
    from datetime import date

    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _flag(request, name: str) -> bool:
    """Read a boolean query parameter.

    `?stalled=false` must be False, and `bool("false")` is True — the classic
    way to ship a filter that is always on.
    """
    return (request.query_params.get(name) or "").strip().lower() in {"1", "true", "yes"}
