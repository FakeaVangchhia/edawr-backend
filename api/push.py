"""Waking a rider's phone when an order lands on it.

The rider app polls its dashboard every fifteen seconds — but only while it is
*open*. Dispatch (`api/dispatch.py`) hands an order to the nearest rider in the
same transaction that marks the bag packed, and the phone it lands on is very
often in a pocket with the screen off. Nothing polls there. The rider found out
when they next looked, and on a fifteen-minute promise that is most of the
promise spent before anyone moved.

This module is the other half: one HTTPS POST to Expo's push gateway, which
hands the message to APNs or FCM, which wakes the handset. `api/models.py`
holds the tokens (`RiderDevice`); `api/views/delivery.py` registers them.

**It is best-effort, in exactly the way `api/audit.py` is.** Every entry point
here swallows its own exceptions and logs them. A notification that cannot be
sent must never fail the request that triggered it: by the time we get here a
manager has marked an order packed and a rider has been assigned, both of them
committed, and refusing that because someone else's push service is having a
bad afternoon would be far worse than a rider learning about the order from the
poll fifteen seconds later. **The poll is still the source of truth.** This is a
prompt to look at the app, never the delivery mechanism for the order itself.

**Why `on_commit`, and why a thread.**

`on_commit` because the interesting callers run inside the transaction that
assigns the order. Sending from in there would buzz a phone about an assignment
that a later `IntegrityError` rolled back, and would hold `select_for_update`
locks on rider rows across a network call to a third party.

A thread because this project has no background worker, and the docstring in
`api/dispatch.py` explains why it does not want one — a queue needs a scheduler,
a broker and something watching all three. Neither does it want a manager's
"mark packed" tap to block on Expo's latency, which is the only other option
once you have ruled a queue out. A daemon thread with a bounded timeout is the
smallest thing that is neither: the request returns immediately, and the worst
case is one thread sitting on a socket for `PUSH_TIMEOUT_SECONDS`.

That trade has a real ceiling. It is sized for one store in Aizawl, where the
notification rate is the order rate — a few hundred a day, spread over opening
hours, one thread each. A second store, or a broadcast to a roster of fifty,
and the right answer becomes a proper queue. Until then, a worker per push is
honest about how little there is to do.

**Why `urllib` and not `requests`.** Two JSON POSTs to one URL is not worth a
dependency. `pyproject.toml` lists what this code imports, and the shorter that
list is the less there is to keep patched.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from functools import partial
from typing import Any, Iterable, Sequence

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from api.models import Order, RiderDevice, User

logger = logging.getLogger(__name__)

# Expo rejects a batch larger than this. Nothing here comes close today — a
# whole roster is a handful of phones — but the roster is the one input that
# grows without anybody editing this file.
MAX_BATCH = 100

# The Android notification channel the rider app creates on launch. It has to
# match `CHANNEL_ID` in mobile/src/push.ts by name, or Android files these under
# a default channel the rider may have silenced without meaning to.
CHANNEL_ID = "orders"

# Expo's word for "this token is dead": the app was uninstalled, or the token
# was rotated. It is the one send failure that is about *us* rather than about
# the network, and the fix is to stop keeping the row.
DEVICE_NOT_REGISTERED = "DeviceNotRegistered"


# --------------------------------------------------------------------------
# What the rider actually reads
# --------------------------------------------------------------------------
def _order_line(order: Order) -> str:
    """The address and the money, which is the whole of what a rider decides on.

    Deliberately not the customer's name or phone number. A notification is
    rendered on a lock screen, which is the one place in the app that is visible
    without unlocking the phone — to whoever is holding it, and to anyone
    standing behind them. The address is unavoidable (it is what the rider needs
    to know whether this is near them); the name and number are not, and they
    are one tap away inside the app.
    """
    address = (order.customer_address or "").strip()
    where = address.splitlines()[0].strip() if address else ""
    money = f"₹{order.grand_total} to collect"
    return f"{where} — {money}" if where else money


def notify_assigned(order: Order, rider: User) -> None:
    """This one is yours. Sent when dispatch or a manager hands over an order.

    The order is already `Dispatched` and already theirs when this fires, so the
    wording is an instruction and not an offer — there is nothing here for the
    rider to accept. Handing it back is still possible, but it happens in the
    app, against a server that will tell them if somebody has moved it since.
    """
    notify_riders(
        [rider],
        title="New delivery assigned",
        body=_order_line(order),
        data={"type": "assigned", "order_id": order.pk},
    )


def notify_pool(order: Order, riders: Sequence[User]) -> None:
    """This is going spare. Sent when nothing was assigned and it is up for grabs.

    The state `api/dispatch.py` calls the honest fallback: automatic assignment
    is off, or found nobody it could give this to, so the order sits in the pull
    feed and the first rider to tap Accept gets it. Everyone in range is told at
    once, because that is exactly what the feed does — this notification is a
    prompt to open it, not a private offer, and the app is where the race is
    settled, with a 409 for whoever is second.
    """
    if not riders:
        return
    notify_riders(
        riders,
        title="Order ready for pickup",
        body=_order_line(order),
        data={"type": "pool", "order_id": order.pk},
    )


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------
def notify_riders(
    riders: Iterable[User],
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Queue a notification to every phone these riders have registered.

    Never raises, never blocks, and does nothing at all unless `PUSH_ENABLED`
    is on. Returns before anything has been sent: the send is scheduled for
    after the current transaction commits, and runs on a worker thread from
    there.
    """
    try:
        if not settings.PUSH_ENABLED:
            return

        rider_ids = [rider.pk for rider in riders if rider is not None]
        if not rider_ids:
            return

        tokens = list(
            RiderDevice.objects.filter(rider_id__in=rider_ids).values_list(
                "expo_token", flat=True
            )
        )
        if not tokens:
            # Nobody has registered a phone. Normal for a fresh deployment, and
            # for a rider who declined the permission prompt.
            return

        messages = [_message(token, title, body, data) for token in tokens]

        # Outside a transaction `on_commit` runs the callback immediately, which
        # is the right behaviour for the few callers that are not in one.
        transaction.on_commit(partial(_deliver, messages))
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("failed to queue a rider notification")


def notify_rider(
    rider: User, *, title: str, body: str, data: dict[str, Any] | None = None
) -> None:
    """One rider. A thin wrapper so call sites read as what they mean."""
    notify_riders([rider], title=title, body=body, data=data)


def _message(token: str, title: str, body: str, data: dict[str, Any] | None) -> dict[str, Any]:
    """One Expo push message.

    `priority: high` and `channelId` are what make Android deliver this promptly
    rather than batching it into a maintenance window — a delivery assignment is
    exactly the "time-sensitive" case that setting exists for, and a fifteen
    minute promise cannot absorb Doze.

    `ttl` is short on purpose. If the handset has been unreachable for five
    minutes the order has either been reassigned or is already late, and a buzz
    about it arriving now sends the rider to an address someone else is standing
    at.
    """
    return {
        "to": token,
        "title": title,
        "body": body,
        "data": data or {},
        "sound": "default",
        "priority": "high",
        "channelId": CHANNEL_ID,
        "ttl": 300,
    }


# --------------------------------------------------------------------------
# The send
# --------------------------------------------------------------------------
def _deliver(messages: list[dict[str, Any]]) -> None:
    """The commit hook, and the guard the `try` in `notify_riders` cannot be.

    `on_commit` callbacks run *after* `notify_riders` has returned, so its own
    `except` is long out of scope by the time this fires — and Django runs
    commit hooks inline on the connection, so anything raised here surfaces in
    the request/response cycle as a 500 for a request whose work already
    committed. A manager would see "mark packed" fail on an order that is packed
    and dispatched.

    That is the exact failure the module docstring promises cannot happen, so
    the promise is kept here rather than trusted to the callee.
    """
    try:
        _send_in_background(messages)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("failed to hand off a rider notification")


def _send_in_background(messages: list[dict[str, Any]]) -> None:
    """Hand the batch to a daemon thread and return.

    Daemon so a shutting-down process is never held open by a notification. The
    cost of losing one in flight at deploy time is a rider seeing the order on
    their next poll; the alternative — a worker that will not exit — is a deploy
    that hangs.
    """
    try:
        threading.Thread(
            target=_send, args=(messages,), name="edawr-push", daemon=True
        ).start()
    except Exception:  # noqa: BLE001
        logger.exception("failed to start the push worker thread")


def _send(messages: list[dict[str, Any]]) -> None:
    """POST the batch to Expo and act on what comes back. Never raises."""
    for start in range(0, len(messages), MAX_BATCH):
        batch = messages[start : start + MAX_BATCH]
        try:
            tickets = _post(batch)
        except Exception:  # noqa: BLE001
            logger.exception("push send failed", extra={"count": len(batch)})
            continue
        try:
            _handle_tickets(batch, tickets)
        except Exception:  # noqa: BLE001
            logger.exception("could not read the push tickets")


def _post(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One request. Returns Expo's per-message tickets, in the order sent."""
    request = urllib.request.Request(
        settings.EXPO_PUSH_URL,
        data=json.dumps(batch).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Only needed once the Expo project turns on enhanced push security;
            # omitted entirely when unset, because an empty bearer is worse than
            # no header at all.
            **(
                {"Authorization": f"Bearer {settings.EXPO_ACCESS_TOKEN}"}
                if settings.EXPO_ACCESS_TOKEN
                else {}
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.PUSH_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))

    # Expo answers `{"data": [...]}` on success and `{"errors": [...]}` when the
    # request itself was wrong — a malformed body, a bad access token. The second
    # is our bug, so it is logged loudly and there are no tickets to act on.
    if isinstance(payload, dict) and payload.get("errors"):
        logger.error("expo rejected the push request", extra={"errors": payload["errors"]})
        return []

    tickets = payload.get("data") if isinstance(payload, dict) else None
    return tickets if isinstance(tickets, list) else []


def _handle_tickets(batch: list[dict[str, Any]], tickets: list[dict[str, Any]]) -> None:
    """Delete the tokens Expo says are dead; log the rest.

    Tickets come back positionally, one per message in the order sent, which is
    the entire reason `notify_riders` builds one message per token rather than
    one message with a list of them. A batched `to` array would return tickets
    we could not map back to a row, and deleting the wrong one silences a phone
    that was working.

    A ticket that is merely `ok` is not proof of delivery — Expo hands the
    message on and reports the real outcome in a *receipt*, fetched separately
    later. Nothing here polls for those: a receipt tells you that a notification
    the rider has already either seen or missed did not arrive, and the only
    action it unlocks is the `DeviceNotRegistered` cleanup this already does on
    the next send. Adding the poller means adding the scheduler this module
    exists to avoid.
    """
    dead: list[str] = []
    for message, ticket in zip(batch, tickets):
        if not isinstance(ticket, dict) or ticket.get("status") == "ok":
            continue
        detail = (ticket.get("details") or {}).get("error")
        if detail == DEVICE_NOT_REGISTERED:
            dead.append(message["to"])
        else:
            # `extra` keys must not collide with LogRecord's own attributes, and
            # `message` is one of them — logging raises a KeyError rather than
            # shadowing it, which would lose this line at the exact moment it is
            # the only record that a notification failed.
            logger.warning(
                "expo could not deliver a notification",
                extra={"error": detail, "expo_message": ticket.get("message")},
            )

    if dead:
        deleted, _ = RiderDevice.objects.filter(expo_token__in=dead).delete()
        logger.info("pruned unregistered rider devices", extra={"count": deleted})


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
def register_device(rider: User, expo_token: str, platform: str = "") -> RiderDevice:
    """Record that `rider` is holding the phone behind `expo_token`.

    **Claims the token rather than adding a row.** `expo_token` is unique across
    the table, so a handset that changes hands at shift change moves to whoever
    signed in last instead of being notified for both riders. `update_or_create`
    on the token is what makes that one statement with no window in which two
    rows exist.
    """
    device, _ = RiderDevice.objects.update_or_create(
        expo_token=expo_token,
        defaults={
            "rider": rider,
            "platform": platform or RiderDevice.UNKNOWN,
            "last_seen_at": timezone.now(),
        },
    )
    return device


def forget_device(rider: User, expo_token: str) -> int:
    """Drop a token at sign-out. Returns how many rows went (0 or 1).

    Scoped to the caller: a rider may only forget a phone that is currently
    registered to them, so a leaked token cannot be used to silence somebody
    else's handset.
    """
    deleted, _ = RiderDevice.objects.filter(rider=rider, expo_token=expo_token).delete()
    return deleted
