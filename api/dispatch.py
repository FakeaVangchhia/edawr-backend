"""Choosing which rider gets an order, without a background worker.

Dispatch used to be a pure pull. An order reaching Ready appeared in every
available rider's feed and whoever tapped Accept first got it; nothing decided
on the store's behalf. `views/delivery.py` still serves that feed, and it is
still what a rider sees when automatic assignment finds nobody — but the store
no longer waits for a tap. `auto_assign` runs the moment an order becomes Ready
and hands it to the nearest eligible rider, in the same transaction.

**Why it assigns outright instead of offering.** A push that offers an order to
one rider at a time needs something to expire an offer nobody answers, and that
something is a scheduler this project does not run (see the module docstring in
`views/delivery.py`, which is why the pull feed was built first). Assigning
outright needs no timer, because there is no pending state to expire: the order
is Dispatched before the request returns. The price is that a rider can be
handed a drop while their phone is in a pocket. The two answers to that already
existed — `is_available`, which the rider controls from the app, and
`POST /api/orders/{id}/assign`, which lets a manager override anything decided
here.

**Nobody eligible is not an error.** The order stays Ready and unassigned, which
is precisely the state `GET /api/orders?stalled=true` was built to surface, and
it drops back into the pull feed for any rider who comes on shift. A dispatch
that raised here would mean a manager could not mark a bag packed at 3am.
"""

from __future__ import annotations

import logging
import math

from django.conf import settings

from api import push
from api.models import AuditLog, Order, OrderRejection, User

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres.

    Straight-line distance, not travel distance — Aizawl is built on ridges and
    the road distance can be several times this. It is used only to decide
    whether an order is *plausibly* in a rider's area, which is a job it does
    well enough; do not present it to anyone as an ETA.

    It lives here rather than in `views/delivery.py`, where it started, because
    both the pull feed and automatic assignment rank riders by it, and a view
    importing a view is the wrong direction. Views import this module; nothing
    here imports a view.
    """
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def reachable_riders(order: Order) -> list[tuple[float, User]]:
    """Riders who could take this order at some point, nearest first.

    On the roster and on shift (`is_active` and `is_available`), inside their
    own `service_radius_km` of the customer, and not among those who declined
    it. **Deliberately ignores who is mid-delivery**: a rider carrying a bag
    right now will be free in a few minutes, so an order waiting on them is
    waiting, not stuck.

    That distinction is the whole point of the split from `eligible_riders`.
    Empty here means nobody is *ever* going to take this order without a
    manager, which is what `GET /api/orders?stalled=true` reports.

    Read-only: it takes no locks, because the stalled list is a GET.
    """
    declined = set(
        OrderRejection.objects.filter(order=order).values_list("rider_id", flat=True)
    )
    on_shift = User.objects.filter(
        role=User.DELIVERY, is_active=True, is_available=True
    ).order_by("pk")
    return _rank(order, (r for r in on_shift if r.pk not in declined))


def eligible_riders(order: Order) -> list[tuple[float, User]]:
    """Every rider who may be handed this order *right now*, nearest first.

    `reachable_riders` minus anyone already carrying a Dispatched order — a
    15-minute promise does not survive stacking two drops on one rider.

    The filter is otherwise deliberately the same one
    `RiderDashboardView._incoming` uses to build the pull feed, because two
    dispatch rules that disagree is how an order becomes invisible in the app
    and assigned in the console.

    Rows are locked with `select_for_update()` in primary-key order, the same
    ordering rule `checkout.py` uses on products: two orders reaching Ready at
    the same instant must not both be told the same rider is free. **Must be
    called inside a transaction.** The lock is a no-op on SQLite, so this is
    correct only once `DATABASE_URL` points at Postgres — as with checkout.
    """
    carrying = set(
        Order.objects.filter(status=Order.DISPATCHED, delivery_boy__isnull=False)
        .values_list("delivery_boy_id", flat=True)
    )
    declined = set(
        OrderRejection.objects.filter(order=order).values_list("rider_id", flat=True)
    )

    candidates = (
        User.objects.select_for_update()
        .filter(role=User.DELIVERY, is_active=True, is_available=True)
        .order_by("pk")
    )
    return _rank(
        order, (r for r in candidates if r.pk not in carrying and r.pk not in declined)
    )


def _rank(order: Order, riders) -> list[tuple[float | None, User]]:
    """Drop riders the order falls outside the radius of, sort the rest by it.

    Distance is computed in Python because haversine is not portable SQL. It is
    straight-line, not travel distance — Aizawl is built on ridges — so it
    decides only whether a drop is *plausibly* this rider's, never an ETA.

    **An order with no coordinates keeps every rider, at distance `None`.**
    Position is optional at checkout: geolocation is opt-in in a browser, and a
    customer who declines it still typed an address a rider can read. The old
    behaviour was worse than either alternative — the columns *defaulted* to the
    store's own coordinates, so such an order measured 0.00 km from everyone and
    won every "nearest first" sort ahead of orders whose distance was real.
    `None` says the radius rule cannot be applied here, rather than pretending
    it passed, and it sorts last so a known-nearby drop is always offered first.
    """
    if order.customer_latitude is None or order.customer_longitude is None:
        return [(None, rider) for rider in riders]

    ranked: list[tuple[float | None, User]] = []
    for rider in riders:
        distance = haversine_km(
            rider.base_latitude,
            rider.base_longitude,
            order.customer_latitude,
            order.customer_longitude,
        )
        if distance <= rider.service_radius_km:
            ranked.append((round(distance, 2), rider))

    ranked.sort(key=lambda pair: pair[0])
    return ranked


def auto_assign(order: Order, request=None) -> User | None:
    """Hand a Ready order to the nearest eligible rider. Returns them, or None.

    Call inside the same transaction that moved the order to Ready, after that
    save. Returns None -- never raises -- when the feature is switched off, the
    order is not Ready, or no rider qualifies; the caller carries on and the
    order stays in the pull feed.
    """
    if not settings.AUTO_ASSIGN_RIDER:
        # Off means the pull feed *is* dispatch, so this is the moment the order
        # becomes visible to everyone in range — and the moment to tell them.
        _notify_pool(order)
        return None
    if order.status != Order.READY or order.delivery_boy_id is not None:
        return None

    ranked = eligible_riders(order)
    if not ranked:
        logger.info("auto-assign found no rider", extra={"order_id": order.pk})
        # Nobody is eligible *right now*, but somebody may be reachable — a
        # rider mid-delivery, about to be free. They see this order in the feed
        # the moment they finish, so the notification is what gets them looking.
        _notify_pool(order)
        return None

    distance, rider = ranked[0]

    # Through the state machine like every other status change, so the
    # dispatched_at stamp lands exactly once and an order that somehow cannot
    # reach Dispatched is left alone rather than forced.
    changed = order.advance_status(Order.DISPATCHED)
    order.delivery_boy = rider
    order.offered_distance_km = distance
    changed += ["delivery_boy", "offered_distance_km"]
    order.save(update_fields=list(dict.fromkeys(changed)))

    logger.info(
        "order auto-assigned",
        extra={"order_id": order.pk, "rider_id": rider.pk, "distance_km": distance},
    )
    # After the save, so a rider is never buzzed about an assignment that did
    # not stick — and `push` defers the send to commit for the same reason,
    # which is why calling it inside this transaction is safe.
    push.notify_assigned(order, rider)
    if request is not None:
        # Recorded under the request that triggered it -- the manager's move to
        # Ready -- because that is the human act the audit trail has to explain.
        from api import audit

        audit.record(
            request, AuditLog.ASSIGN, "order", order.pk,
            f"Auto-assigned order #{order.pk} to {rider.name} ({distance} km)",
            {"delivery_boy_id": [None, rider.pk], "auto": True},
        )
    return rider


def decline_for(order: Order, rider: User) -> None:
    """Record that `rider` is not to be given `order` again.

    Used when a rider hands a Dispatched order back to Ready. Without it,
    automatic assignment re-picks whoever is nearest -- which is very often the
    rider who just handed it back -- and the order ping-pongs between the two
    of them until someone opens the console. `get_or_create` keeps a repeated
    hand-back idempotent rather than an integrity error, exactly as the reject
    endpoint does.
    """
    OrderRejection.objects.get_or_create(order=order, rider=rider)


def _notify_pool(order: Order) -> None:
    """Tell everyone who would see this order in their feed that it is there.

    Called only on the paths where nothing was assigned, which is the one case
    the pull feed still serves. Deliberately uses `reachable_riders` — the
    read-only, lock-free query — rather than `eligible_riders`: this runs inside
    the transaction that moved the order to Ready, and taking `select_for_update`
    locks on the whole roster to decide who to *tell* would be a lock held
    across work that has nothing to do with the order.

    The difference between the two is riders currently carrying a bag, and
    including them is the point rather than a bug. They are minutes from free,
    the order will still be sitting there, and a rider who knows what is waiting
    plans the ride back. `push.notify_pool` is a prompt to open the app; the
    feed decides what they actually see when they do.
    """
    push.notify_pool(order, [rider for _, rider in reachable_riders(order)])
