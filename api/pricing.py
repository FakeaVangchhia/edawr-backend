"""What an order costs, and when it is promised.

Every number a customer is charged is computed here and nowhere else. The client
sends quantities and product ids; it never sends a price, a fee or a total, and
the server never reads one from the request. That is not a style preference —
a checkout that trusts a client-supplied total is a checkout where the customer
picks the price.

**Rounding is explicit.** `Decimal` arithmetic is exact but not automatically
2dp: `Decimal("62.10") * 3` is `186.30`, but a percentage discount would produce
`186.2999...`. Every value that leaves this module goes through `money()`, which
quantises to two places with ROUND_HALF_UP — the rule people are taught in
school, and the one a customer checking the arithmetic by hand will apply.
Python's default is ROUND_HALF_EVEN, which rounds 0.125 to 0.12 and would make
the bill look wrong for reasons nobody wants to explain.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")


def money(value: Decimal | int | float | str) -> Decimal:
    """Coerce to a 2dp Decimal, rounding half up.

    Accepts float only because DRF hands one over when a serializer field is
    declared as a FloatField somewhere upstream; converting via `str()` first
    avoids inheriting the binary representation error (`Decimal(0.1)` is
    0.1000000000000000055511151231257827, `Decimal("0.1")` is 0.1).
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Charges:
    """The bill for one basket. Frozen because nothing should adjust it in place."""

    items_total: Decimal
    delivery_fee: Decimal
    handling_fee: Decimal
    grand_total: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "items_total": self.items_total,
            "delivery_fee": self.delivery_fee,
            "handling_fee": self.handling_fee,
            "grand_total": self.grand_total,
        }


def compute_charges(items_total: Decimal) -> Charges:
    """Turn a basket subtotal into the full bill.

    Delivery is free above `FREE_DELIVERY_ABOVE`, which is the lever that makes
    a customer add one more thing. The handling fee applies to every order and
    is what covers picking — it is shown as its own line rather than buried in
    product prices, because a fee a customer can see is a fee they do not
    dispute later.
    """
    items_total = money(items_total)

    delivery_fee = (
        ZERO
        if items_total >= money(settings.FREE_DELIVERY_ABOVE)
        else money(settings.DELIVERY_FEE)
    )
    handling_fee = money(settings.HANDLING_FEE)

    # An empty basket costs nothing. Charging a handling fee on a zero-item
    # order would be indefensible, and the checkout rejects it anyway — this is
    # belt and braces so no caller can construct that bill.
    if items_total <= ZERO:
        return Charges(ZERO, ZERO, ZERO, ZERO)

    return Charges(
        items_total=items_total,
        delivery_fee=delivery_fee,
        handling_fee=handling_fee,
        grand_total=money(items_total + delivery_fee + handling_fee),
    )


def free_delivery_shortfall(items_total: Decimal) -> Decimal:
    """How much more is needed to earn free delivery; zero once earned.

    The storefront shows this as "Add ₹43 more for free delivery", so it lives
    here beside the rule it depends on rather than being re-derived in the UI
    where it could drift out of step with `compute_charges`.
    """
    threshold = money(settings.FREE_DELIVERY_ABOVE)
    items_total = money(items_total)
    if items_total >= threshold:
        return ZERO
    return money(threshold - items_total)
