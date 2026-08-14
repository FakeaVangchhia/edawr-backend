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
class DeliveryTier:
    """One delivery speed: what it costs and what it promises.

    The fee and the window are one object because they are one decision. A
    customer trading ten rupees for half an hour is choosing between two
    complete offers, not setting two independent dials, and splitting them
    across two settings lookups is how they drift out of step.
    """

    key: str
    label: str
    fee: Decimal
    promise_minutes: int


def delivery_tiers() -> tuple[DeliveryTier, ...]:
    """Every tier the store offers, fastest first.

    Built on each call rather than at import so `@override_settings` works in
    tests, and so a settings reload does not leave a stale price frozen into a
    module-level constant.
    """
    return (
        DeliveryTier(
            key="instant",
            label="Instant",
            fee=money(settings.DELIVERY_FEE_INSTANT),
            promise_minutes=int(settings.DELIVERY_PROMISE_MINUTES_INSTANT),
        ),
        DeliveryTier(
            key="slow",
            label="Slow",
            fee=money(settings.DELIVERY_FEE_SLOW),
            promise_minutes=int(settings.DELIVERY_PROMISE_MINUTES_SLOW),
        ),
    )


def default_tier() -> DeliveryTier:
    """The tier a request that names none is given."""
    tiers = delivery_tiers()
    wanted = str(settings.DEFAULT_DELIVERY_TYPE)
    for tier in tiers:
        if tier.key == wanted:
            return tier
    # A misconfigured DEFAULT_DELIVERY_TYPE must not take the store down, and
    # the first tier is the fastest — the safe direction to be wrong in.
    return tiers[0]


def resolve_tier(key: str | None) -> DeliveryTier:
    """Turn a tier key into a tier, falling back to the default.

    The serializer already rejects an unknown key with a 400, so the fallback
    here is for the internal callers that pass `None` (the quote endpoint
    before a customer has chosen, the seed command). It resolves *upward* to
    the default rather than downward to the cheapest: quietly giving someone a
    slower delivery than they think they bought is the worse mistake.
    """
    if not key:
        return default_tier()
    for tier in delivery_tiers():
        if tier.key == key:
            return tier
    return default_tier()


@dataclass(frozen=True)
class Charges:
    """The bill for one basket. Frozen because nothing should adjust it in place."""

    items_total: Decimal
    delivery_fee: Decimal
    handling_fee: Decimal
    grand_total: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        """The money columns of an Order, ready to splat into `create()`.

        Deliberately money only. `delivery_type` and `promised_minutes` are
        passed to `Order.objects.create()` separately, because the moment this
        dict carries a non-money field it stops being obvious that every key
        here has to be a Decimal column on the model.
        """
        return {
            "items_total": self.items_total,
            "delivery_fee": self.delivery_fee,
            "handling_fee": self.handling_fee,
            "grand_total": self.grand_total,
        }


def compute_charges(items_total: Decimal, delivery_type: str | None = None) -> Charges:
    """Turn a basket subtotal into the full bill.

    Delivery is free above `FREE_DELIVERY_ABOVE` — the lever that makes a
    customer add one more thing — and that threshold applies to every tier, so
    the choice of speed stops mattering once the basket is large enough. Below
    it, the chosen tier's fee applies. The handling fee applies to every order
    and is what covers picking; it is shown as its own line rather than buried
    in product prices, because a fee a customer can see is a fee they do not
    dispute later.
    """
    items_total = money(items_total)
    tier = resolve_tier(delivery_type)

    delivery_fee = (
        ZERO if items_total >= money(settings.FREE_DELIVERY_ABOVE) else tier.fee
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
