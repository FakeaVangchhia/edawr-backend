"""Recording and reading live positions.

Three writers and two readers, kept out of the views because the same rules are
reached from four routes and the interesting parts are not HTTP.

**What this module is not.** It does not decide who may see a position — that is
`api/permissions.py` and the route itself — and it does not feed dispatch.
`api/dispatch.py` still ranks riders by their static home base; see the comment
above `RiderLocation` in `api/models.py` for why moving it to live positions is
a separate decision rather than an obvious improvement.

**Distance here is straight-line and is never an ETA.** `haversine_km` is
imported from `api/dispatch.py` rather than reimplemented, and it carries the
same caveat it carries there: Aizawl is built on ridges, so two kilometres of
map can be a quarter of an hour of riding. Every number this module produces is
a distance, and the time promise stays `Order.promised_at`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from api.dispatch import haversine_km
from api.models import Order, OrderCustomerLocation, OrderLocationPing, RiderLocation, User


def active_order_for(rider: User) -> Order | None:
    """The order this rider is currently carrying, if any.

    `Dispatched` is the only status that counts as carrying: an order still
    Ready has not left the store, and one Delivered is over. This is the same
    definition `dispatch.eligible_riders` uses to decide a rider is busy, and
    it must stay that way — if the two disagree, a rider gets a second drop
    while the first is still on the bike.
    """
    return (
        Order.objects.filter(delivery_boy_id=rider.id, status=Order.DISPATCHED)
        .order_by("-id")
        .first()
    )


@transaction.atomic
def record_rider_position(
    rider: User,
    *,
    latitude: float,
    longitude: float,
    accuracy_m: float | None = None,
    speed_kmh: float | None = None,
    heading: float | None = None,
    recorded_at: datetime | None = None,
) -> tuple[RiderLocation, Order | None]:
    """Store one fix from a rider's handset. Returns it and the order it belongs to.

    Two writes, and they differ on purpose:

    - `RiderLocation` is **always** updated, whether or not the rider is
      carrying anything. A rider between drops still has a position, and the
      console showing where everyone is — including who is idle and where — is
      most of the value of a dispatch map.
    - `OrderLocationPing` is appended **only** while an order is actually out
      for delivery. That is what keeps "we track during a delivery" a true
      statement about the stored data rather than about the app's intentions:
      an off-shift rider who leaves the app open leaves no trail behind.

    Atomic because the two must agree. A ping whose order is not the one the
    current position records would be a trail that disagrees with the map.
    """
    order = active_order_for(rider)

    location, _ = RiderLocation.objects.update_or_create(
        rider=rider,
        defaults={
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": accuracy_m,
            "speed_kmh": speed_kmh,
            "heading": heading,
            "order": order,
            "recorded_at": recorded_at,
            "received_at": timezone.now(),
        },
    )

    if order is not None:
        OrderLocationPing.objects.create(
            order=order,
            rider=rider,
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy_m,
            recorded_at=recorded_at,
        )

    return location, order


def record_customer_position(
    order: Order,
    *,
    latitude: float,
    longitude: float,
    accuracy_m: float | None = None,
) -> OrderCustomerLocation:
    """Store where the customer says they are waiting.

    One row per order, overwritten. **It does not touch
    `Order.customer_latitude`** — that is the checkout position, it is what the
    radius check and dispatch were decided on, and a fix taken later from a
    moving car must not be able to rewrite it.
    """
    location, _ = OrderCustomerLocation.objects.update_or_create(
        order=order,
        defaults={
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": accuracy_m,
            "received_at": timezone.now(),
        },
    )
    return location


def distance_to_customer(location: RiderLocation, order: Order) -> float | None:
    """Straight-line kilometres from a rider to an order's delivery address.

    **`None` when the order has no coordinates, and never `0.0`.** Position is
    optional at checkout and declining it is supported, so "we do not know how
    far away the rider is" is a real answer that has to survive being
    serialised. Returning zero instead is the exact bug `dispatch._rank`
    documents: the columns used to default to the store's own position, every
    rider measured 0.00 km away, and the rider app rendered that as fact.
    """
    if order.customer_latitude is None or order.customer_longitude is None:
        return None
    distance = haversine_km(
        location.latitude,
        location.longitude,
        order.customer_latitude,
        order.customer_longitude,
    )
    # Two decimals, matching `dispatch._rank`, so the same drop does not read as
    # 1.8 km in the console and 1.7961 km on the tracking page.
    return round(distance, 2)


def rider_position_for_tracking(order: Order) -> dict | None:
    """What the customer's tracking page may see, or `None`.

    `None` — rather than an error — for every reason the answer is no, because
    from the page's point of view they are one situation: there is nothing to
    draw yet. The four gates:

    1. the order is **Dispatched**. Before that nobody is carrying it, and
       afterwards it is over. This is what stops the tracking token from
       becoming a way to watch a rider's whole shift;
    2. a rider is assigned;
    3. that rider has ever reported a position;
    4. the last one is **fresh**. A marker from eleven minutes ago is not a live
       position, and drawing it as one is worse than drawing nothing: the
       customer stands at the window watching a rider who is not there.
    """
    if order.status != Order.DISPATCHED or order.delivery_boy_id is None:
        return None

    location = RiderLocation.objects.filter(rider_id=order.delivery_boy_id).first()
    if location is None or location.is_stale:
        return None

    return {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "heading": location.heading,
        "received_at": location.received_at,
        "distance_km": distance_to_customer(location, order),
    }


def console_roster() -> list[dict]:
    """Every active rider and their last known position, for the console map.

    Built from the rider roster outward rather than from the location table, so
    a rider who has never reported still appears with nulls. A map that silently
    omits three riders is worse than one that shows them greyed out: the manager
    cannot tell "not tracked" from "not working" and will read the gap as the
    latter.

    Stale rows are **returned, flagged, not hidden.** "Last seen 4 minutes ago
    near Chanmari" is the answer to where somebody is when their phone is in a
    pocket, and it is the answer a manager wants. Only the customer-facing path
    suppresses a stale fix, because there the marker would be read as live.
    """
    riders = (
        User.objects.filter(role=User.DELIVERY, is_active=True)
        .select_related("location")
        .order_by("name", "id")
    )

    roster = []
    for rider in riders:
        location = getattr(rider, "location", None)
        if location is None:
            roster.append(
                {
                    "id": rider.id,
                    "name": rider.name,
                    "phone": rider.phone,
                    "is_available": rider.is_available,
                    "latitude": None,
                    "longitude": None,
                    "accuracy_m": None,
                    "speed_kmh": None,
                    "heading": None,
                    "received_at": None,
                    "age_seconds": None,
                    "is_stale": True,
                    "order_id": None,
                }
            )
            continue

        roster.append(
            {
                "id": rider.id,
                "name": rider.name,
                "phone": rider.phone,
                "is_available": rider.is_available,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "accuracy_m": location.accuracy_m,
                "speed_kmh": location.speed_kmh,
                "heading": location.heading,
                "received_at": location.received_at,
                "age_seconds": int(location.age_seconds),
                "is_stale": location.is_stale,
                "order_id": location.order_id,
            }
        )
    return roster


def prune(now: datetime | None = None) -> tuple[int, int]:
    """Delete expired breadcrumbs and orphaned customer positions.

    Returns `(pings, customer_positions)`. Called by `manage.py prune_locations`;
    see `settings.LOCATION_PING_RETENTION_DAYS`.

    The second number should normally be zero. `Order.advance_status` deletes a
    customer's position the moment the order ends, so a row here belongs to an
    order that reached a terminal status some other way — a bulk update, a
    migration, a fixture — and this is the backstop that catches it rather than
    the mechanism that is relied on.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.LOCATION_PING_RETENTION_DAYS)

    pings, _ = OrderLocationPing.objects.filter(received_at__lt=cutoff).delete()
    orphans, _ = OrderCustomerLocation.objects.filter(
        order__status__in=Order.TERMINAL
    ).delete()
    return pings, orphans
