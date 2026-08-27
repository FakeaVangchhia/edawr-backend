"""Turning a basket into an order.

This module owns the one operation in the system that must not be half-done.
Placing an order writes an `Order`, writes N `OrderItem` rows, and decrements N
product stock counts. If any part of that lands without the others you get an
order nobody can fulfil, or stock that vanishes without a sale behind it. It is
therefore all inside one `transaction.atomic()` block.

**Two customers, one last packet of biscuits.** The dangerous interleaving is:

    request A: reads stock = 1     request B: reads stock = 1
    request A: 1 >= 1, ok          request B: 1 >= 1, ok
    request A: writes stock = 0    request B: writes stock = 0

Both orders are accepted, one packet exists, and the stock count says zero
rather than the -1 that would at least have made the problem visible.
`select_for_update()` closes it: the first transaction to reach the row holds a
lock until it commits, so the second one blocks at the *read* and sees stock = 0.

That protection is real on Postgres and a no-op on SQLite, which has no row
locks and serialises whole write transactions instead. SQLite happens to be safe
here for the cruder reason that only one write transaction runs at a time — but
that is a property of the toy database, not of this code, which is why the lock
is written out properly.

**Rows are locked in a fixed order.** Two baskets containing the same two
products in opposite orders would otherwise be able to take each other's locks
and deadlock. Sorting by primary key means every transaction in the system grabs
those rows in the same sequence, so one always finishes first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from api.exceptions import Conflict, StoreClosed
from api.dispatch import haversine_km
from api.models import Order, OrderItem, Product, StoreSettings
from api.pricing import Charges, DeliveryTier, compute_charges, money, resolve_tier

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


class BasketUnavailable(Exception):
    """One or more requested items cannot be supplied.

    Deliberately *not* a DRF `ValidationError`. DRF normalises the payload of
    that exception by running every leaf through `force_str`, which would turn
    `"product_id": 7` into `"product_id": "7"` — and the storefront needs to
    match those ids against its cart, where they are numbers. Carrying the list
    on a plain exception lets the view render it untouched.
    """

    def __init__(self, unavailable: list[dict]):
        self.unavailable = unavailable
        names = ", ".join(item["name"] for item in unavailable)
        self.detail = f"Some items are no longer available: {names}."
        super().__init__(self.detail)


@dataclass(frozen=True)
class BasketLine:
    product: Product
    quantity: int

    @property
    def line_total(self) -> Decimal:
        return money(self.product.price * self.quantity)


@dataclass(frozen=True)
class Basket:
    lines: list[BasketLine]
    # Requested products that cannot be supplied, each as
    # {"product_id": int, "name": str, "reason": str, "available": int}.
    # Returned rather than raised by `read_basket` so the quote endpoint can
    # show the customer what to change before they try to check out.
    unavailable: list[dict]

    # The delivery speed this basket is being priced at. Carried on the basket
    # rather than passed alongside it so `charges` stays a property and there is
    # no way to price a basket at one tier and store it as another.
    delivery_type: str = ""

    @property
    def items_total(self) -> Decimal:
        return money(sum((line.line_total for line in self.lines), ZERO))

    @property
    def tier(self) -> DeliveryTier:
        return resolve_tier(self.delivery_type)

    @property
    def charges(self) -> Charges:
        return compute_charges(self.items_total, self.delivery_type)


def read_basket(
    items: list[dict], *, lock: bool = False, delivery_type: str | None = None
) -> Basket:
    """Resolve `[{product_id, quantity}]` against the catalogue.

    With `lock=True` the product rows are locked for the rest of the
    transaction, which is what makes the subsequent stock check meaningful.
    Callers that only want a price (the quote endpoint) pass False and take no
    locks — quoting is a read, and holding write locks for it would serialise
    every browsing customer behind every checking-out one.
    """
    requested = {line["product_id"]: line["quantity"] for line in items}
    if not requested:
        raise ValidationError("Your basket is empty.")

    # Sorted for deterministic lock ordering — see the module docstring.
    queryset = Product.objects.filter(id__in=sorted(requested)).order_by("id")
    if lock:
        queryset = queryset.select_for_update()

    found = {product.id: product for product in queryset}

    lines: list[BasketLine] = []
    unavailable: list[dict] = []

    for product_id in sorted(requested):
        quantity = requested[product_id]
        product = found.get(product_id)

        if product is None:
            unavailable.append({
                "product_id": product_id,
                "name": "Unknown item",
                "reason": "This item is no longer in the catalogue.",
                "available": 0,
            })
            continue

        # An inactive product is one the store has withdrawn. It stays in the
        # database for order history but must not be sellable, and the customer
        # is told it is unavailable rather than that it does not exist —
        # they are looking at a stale tab, not making it up.
        if product.status != Product.ACTIVE:
            unavailable.append({
                "product_id": product_id,
                "name": product.name,
                "reason": "This item is not available right now.",
                "available": 0,
            })
            continue

        if product.stock < quantity:
            unavailable.append({
                "product_id": product_id,
                "name": product.name,
                "reason": (
                    "Out of stock."
                    if product.stock == 0
                    else f"Only {product.stock} left."
                ),
                "available": max(product.stock, 0),
            })
            continue

        lines.append(BasketLine(product=product, quantity=quantity))

    return Basket(
        lines=lines,
        unavailable=unavailable,
        delivery_type=resolve_tier(delivery_type).key,
    )


def quote(items: list[dict], delivery_type: str | None = None) -> tuple[Basket, Charges]:
    """Price a basket without touching it. Used by the cart drawer."""
    basket = read_basket(items, lock=False, delivery_type=delivery_type)
    return basket, basket.charges


def place_order(
    data: dict, idempotency_key: str = "", *, customer=None
) -> tuple[Order, bool]:
    """Create an order, or return the one an earlier copy of this request made.

    Returns `(order, created)`. The view answers **201** when it created
    something and **200** when it is handing back a replay, because a replay did
    not create anything and saying it did would be a lie a client could act on.

    **Why this wrapper exists.** Checkout writes an order, N item rows, and N
    stock decrements. A customer on Aizawl mobile data whose request times out
    *after* the server committed will retry — the browser may retry for them —
    and without a key the second request is a second order: charged twice, and
    the shelf decremented twice for goods that will be packed once.

    The three cases, in the order they are handled:

    1. **The sequential retry.** The first request finished and committed. A
       plain lookup finds it. This is deliberately *outside* the transaction
       below: putting it inside would mean the dedupe read happens while the
       basket's product rows are locked, so every retry would hold locks that
       every other customer's checkout then queues behind.
    2. **The first attempt.** Nothing matches; do the work.
    3. **The concurrent retry.** The first request is still in flight, so it has
       committed nothing yet and step 1 saw nothing. The unique constraint on
       `Order.idempotency_key` is what catches this one — the loser's INSERT
       fails, its transaction rolls back whole (no order, no stock movement),
       and it re-reads to find the winner. That re-read has to be here rather
       than inside the `except`, because a rolled-back atomic block cannot run
       another query.

    A key is optional. Without one this is exactly the behaviour it always had.

    **`customer` does not enter the dedupe read, and a replay does not acquire
    one.** Both are deliberate. Adding a `customer` term to the lookup below
    would let one key create two orders — a guest attempt and a signed-in retry
    would stop matching each other — which is precisely the double-charge the
    key exists to prevent. And a replay is not an opportunity to improve the
    order it is handing back: it created nothing and changes nothing, which is
    what the 200-rather-than-201 answer means. The only way to reach that case
    is to sign in between a checkout POST and its own retry.
    """
    key = (idempotency_key or "").strip()

    if key:
        existing = Order.objects.filter(idempotency_key=key).first()
        if existing is not None:
            logger.info(
                "checkout replay served",
                extra={"order_id": existing.pk, "idempotency_key": key},
            )
            return existing, False

    try:
        return _place_order(data, key, customer=customer), True
    except IntegrityError:
        if not key:
            raise
        # The concurrent retry. Somebody else won the race with this same key,
        # and by the time we get here their transaction has committed — the
        # unique violation is the proof of it.
        winner = Order.objects.filter(idempotency_key=key).first()
        if winner is None:  # pragma: no cover — a different constraint failed
            raise
        logger.info(
            "checkout replay served after race",
            extra={"order_id": winner.pk, "idempotency_key": key},
        )
        return winner, False


@transaction.atomic
def _place_order(data: dict, idempotency_key: str = "", *, customer=None) -> Order:
    """Create an order from validated checkout data, or raise ValidationError.

    `data` is the output of `CheckoutSerializer`, so the customer fields are
    already normalised and the item list already has duplicate product ids
    merged. Everything financial is derived here from the catalogue — nothing
    about money is read from the request.

    Call `place_order` rather than this. Everything here is one transaction, so
    a raise anywhere below leaves no order, no items and no stock movement.
    """
    # Two gates before anything is locked or written, because both are refusals
    # about the order as a whole and neither needs the catalogue.
    store = StoreSettings.load()

    # 1. Is the shop open? There were no opening hours and no kill switch at
    #    all, so a 03:00 order was accepted, promised in fifteen minutes, and
    #    left on a board nobody was watching.
    closed = store.closed_reason()
    if closed:
        raise StoreClosed(closed)

    # 2. Can we actually get there? `CheckoutSerializer` accepts any latitude in
    #    -90..90, and nothing checked it against a service area. An address in
    #    Mumbai was accepted, charged, and given a 15-minute Aizawl promise; it
    #    then never appeared in any rider's feed, so it surfaced only once
    #    `is_late` tripped it into the stalled queue -- after the customer had
    #    waited out the entire promise. `api/validators.py` already restricts
    #    phone numbers to India on exactly this reasoning.
    #
    #    An order with no position at all is still accepted. Geolocation is
    #    opt-in in the browser and a customer who declines it is not a customer
    #    to turn away; the address is still typed, and the rider still reads it.
    latitude = data.get("customer_latitude")
    longitude = data.get("customer_longitude")
    if latitude is not None and longitude is not None:
        distance = haversine_km(
            store.store_latitude, store.store_longitude, latitude, longitude
        )
        if distance > store.delivery_radius_km:
            raise ValidationError(
                f"That address is about {distance:.1f} km from the store, and we "
                f"deliver within {store.delivery_radius_km:g} km. "
                "Please choose an address inside the delivery area."
            )

    basket = read_basket(
        data["items"], lock=True, delivery_type=data.get("delivery_type")
    )

    # Raising rolls the transaction back, so the locks are released and nothing
    # is written. The view turns this into a 409 carrying the item list, which
    # is what lets the storefront grey out exactly the offending rows.
    if basket.unavailable:
        raise BasketUnavailable(basket.unavailable)

    if not basket.lines:
        raise ValidationError("Your basket is empty.")

    items_total = basket.items_total
    minimum = money(settings.MIN_ORDER_VALUE)
    if items_total < minimum:
        raise ValidationError(
            f"Minimum order value is ₹{minimum}. Add ₹{money(minimum - items_total)} more."
        )

    charges = basket.charges
    tier = basket.tier

    order = Order.objects.create(
        # Empty string means "no key given", and it must be stored as NULL, not
        # as "". Postgres treats NULLs as distinct in a unique index but two
        # empty strings as a duplicate — so a literal "" here would let exactly
        # one key-less order ever exist and 500 on the second.
        idempotency_key=idempotency_key or None,
        # The account, when the caller was signed in. It comes from the token in
        # the view and never from `data` — the checkout body is the money
        # boundary and carries ids, quantities and contact details only, and a
        # customer id in it would be a customer id anybody could type. Same rule
        # the rider endpoints follow for the same reason.
        #
        # None is the ordinary case: guest checkout, unchanged in every respect.
        customer=customer,
        customer_name=data["customer_name"],
        customer_phone=data["customer_phone"],
        customer_address=data["customer_address"],
        customer_landmark=data.get("customer_landmark") or None,
        delivery_notes=data.get("delivery_notes") or None,
        # None when the customer did not share a position. NULL rather than the
        # store's coordinates, so "unknown" and "next door" stay different
        # things -- see the note on the model field.
        customer_latitude=latitude,
        customer_longitude=longitude,
        payment_method=data.get("payment_method", Order.COD),
        status=Order.PLACED,
        # Both snapshotted from the tier at this moment, so re-tuning that tier
        # later never rewrites what this customer was told. They are set here
        # rather than inside `charges.as_dict()` because that dict is the money
        # columns and nothing else — see `Charges.as_dict`.
        delivery_type=tier.key,
        promised_minutes=tier.promise_minutes,
        **charges.as_dict(),
    )

    OrderItem.objects.bulk_create([
        OrderItem(
            order=order,
            product=line.product,
            quantity=line.quantity,
            # Copied, not referenced. A price change tomorrow must not restate
            # what this customer was charged today.
            name=line.product.name,
            price=line.product.price,
            mrp=line.product.mrp,
            unit=line.product.unit,
            image_url=line.product.image_url,
            line_total=line.line_total,
        )
        for line in basket.lines
    ])

    for line in basket.lines:
        line.product.stock -= line.quantity
    Product.objects.bulk_update([line.product for line in basket.lines], ["stock"])

    logger.info(
        "order placed",
        extra={
            "order_id": order.pk,
            "items": len(basket.lines),
            "grand_total": str(order.grand_total),
        },
    )
    return order


@transaction.atomic
def cancel_order(order: Order, reason: str = "") -> Order:
    """Cancel an order and put its stock back.

    Only legal while the goods are still in the store (`Order.CANCELLABLE`).
    Once a rider has it, cancelling would return stock that has physically left
    the building.

    The order row is re-read under a lock first: without that, two cancel
    requests racing each other both pass the status check and both restore the
    stock, inventing inventory that does not exist.
    """
    locked = Order.objects.select_for_update().get(pk=order.pk)

    if locked.status not in Order.CANCELLABLE:
        # 409, not 400. The body was fine; the order has moved on. See
        # api/exceptions.Conflict for why this matters enough to have its own
        # class -- every other illegal move in the system already answers 409.
        raise Conflict(
            f"An order that is already {locked.get_status_display().lower()} "
            "cannot be cancelled."
        )

    changed = locked.advance_status(Order.CANCELLED)
    locked.cancellation_reason = reason.strip() or "Cancelled"
    changed.append("cancellation_reason")
    locked.save(update_fields=changed)

    _return_stock(locked)

    logger.info(
        "order cancelled",
        extra={"order_id": locked.pk, "reason": locked.cancellation_reason},
    )
    return locked


def _return_stock(order: Order) -> None:
    """Put an order's units back on the shelf.

    Locked in primary-key order for the same deadlock reason as checkout, and
    read fresh rather than trusting the denormalised item rows — `OrderItem`
    copies the name and price at purchase time deliberately, and its quantity is
    the only column here worth believing.

    Shared by cancellation and by the return of a failed delivery, which is why
    it is a function rather than a loop inside `cancel_order`. Two copies of
    "give the stock back" is exactly the kind of duplication that ends with one
    of them being fixed.

    **Must be called inside a transaction**; it takes row locks and does not
    open one of its own.
    """
    items = list(order.items.all())
    if not items:
        return

    products = {
        product.id: product
        for product in Product.objects.select_for_update()
        .filter(id__in=sorted(item.product_id for item in items))
        .order_by("id")
    }
    for item in items:
        product = products.get(item.product_id)
        if product is not None:
            product.stock += item.quantity
    Product.objects.bulk_update(products.values(), ["stock"])


@transaction.atomic
def restock_failed_order(order: Order) -> Order:
    """Return the goods from a failed delivery to the shelf.

    **Deliberately a separate step from marking the order Failed**, and this is
    the crux of the whole feature. When a rider reports a refused delivery the
    bag is with the rider, on a bike, somewhere in Aizawl. Restocking at that
    moment would put units into the catalogue that are not in the building, and
    the next customer would buy something the store cannot pick.

    So `Dispatched -> Failed` records the outcome and moves no stock, and this
    is what a manager calls when the goods are physically back. `restocked_at`
    is what makes it happen exactly once: two clicks, or two managers, must not
    double the inventory.

    Cancellation does not need the split, because a cancelled order's goods
    never left the store.
    """
    locked = Order.objects.select_for_update().get(pk=order.pk)

    if locked.status != Order.FAILED:
        raise Conflict(
            "Only a failed delivery can be restocked. Cancelling an order "
            "returns its stock already."
        )
    if locked.restocked_at is not None:
        raise Conflict("This order's stock has already been returned.")

    _return_stock(locked)
    locked.restocked_at = timezone.now()
    locked.save(update_fields=["restocked_at"])

    logger.info("failed order restocked", extra={"order_id": locked.pk})
    return locked
