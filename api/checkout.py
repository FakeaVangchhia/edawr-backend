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
from django.db import transaction
from rest_framework.exceptions import ValidationError

from api.models import Order, OrderItem, Product
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


@transaction.atomic
def place_order(data: dict) -> Order:
    """Create an order from validated checkout data, or raise ValidationError.

    `data` is the output of `CheckoutSerializer`, so the customer fields are
    already normalised and the item list already has duplicate product ids
    merged. Everything financial is derived here from the catalogue — nothing
    about money is read from the request.
    """
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
        customer_name=data["customer_name"],
        customer_phone=data["customer_phone"],
        customer_address=data["customer_address"],
        customer_landmark=data.get("customer_landmark") or None,
        delivery_notes=data.get("delivery_notes") or None,
        customer_latitude=data.get("customer_latitude", 23.7272),
        customer_longitude=data.get("customer_longitude", 92.7178),
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
        raise ValidationError(
            f"An order that is already {locked.get_status_display().lower()} "
            "cannot be cancelled."
        )

    changed = locked.advance_status(Order.CANCELLED)
    locked.cancellation_reason = reason.strip() or "Cancelled"
    changed.append("cancellation_reason")
    locked.save(update_fields=changed)

    # Restock. Locked in primary-key order for the same deadlock reason as
    # checkout, and read fresh rather than trusting the denormalised item rows.
    items = list(locked.items.all())
    if items:
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

    logger.info(
        "order cancelled",
        extra={"order_id": locked.pk, "reason": locked.cancellation_reason},
    )
    return locked
