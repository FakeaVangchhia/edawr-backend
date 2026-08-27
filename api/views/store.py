"""The public storefront: catalogue, basket pricing, checkout and tracking.

Everything in this module is reachable without a token, which makes the module
boundary itself a security control. `products.py` inherits `AdminAPIView` and is
admin-only; nothing here does, and nothing here may return a serializer that
carries staff-only data. That is why the catalogue uses
`StoreProductSerializer` rather than the `ProductSerializer` the admin console
gets — cost price, supplier and shelf location are not the customer's business,
and keeping them out is a property of the class rather than of somebody
remembering to exclude a field.

Order tracking is public too, and that needs justifying. A customer here has no
account, so there is nothing to authenticate them *with*. Instead the order
carries a 190-bit `tracking_token` handed back once at checkout: possession of
the token is the credential. The endpoint is keyed on that token and never on
the order id, so there is no sequence to walk.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from api.checkout import BasketUnavailable, cancel_order, place_order, quote
from api.models import Category, Customer, Order, OrderItem, Product, StoreSettings
from api.paging import read_page
from api.pricing import default_tier, delivery_tiers, money, resolve_tier
from api.serializers import (
    BasketQuoteSerializer,
    CancelOrderSerializer,
    CheckoutSerializer,
    CustomerLocationReportSerializer,
    OrderTrackingSerializer,
    StoreCategorySerializer,
    StoreConfigSerializer,
    StoreProductSerializer,
    TrackedRiderLocationSerializer,
)
from api import location as location_service

# Orders are always rendered with their items; without the prefetch the nested
# serializer issues one query per order.
TRACKED_ORDERS = Order.objects.prefetch_related("items").select_related("delivery_boy")


class StoreConfigView(APIView):
    """GET /api/store/config — the promise, the tiers and the fees, from one source.

    The storefront renders "delivery in 15 minutes", "free delivery over ₹199",
    a fee line in the cart and the two options in the speed picker. Hardcoding
    any of those in React means they go stale the day someone edits the
    environment, and the customer is then quoted one number and charged another.
    """

    @extend_schema(responses=StoreConfigSerializer)
    def get(self, request):
        fallback = default_tier()
        store = StoreSettings.load()
        return Response(
            StoreConfigSerializer(
                {
                    "store_name": settings.STORE_NAME,
                    "store_city": settings.STORE_CITY,
                    "delivery_tiers": [
                        {
                            "key": tier.key,
                            "label": tier.label,
                            "fee": tier.fee,
                            "promise_minutes": tier.promise_minutes,
                        }
                        for tier in delivery_tiers()
                    ],
                    "free_delivery_above": money(settings.FREE_DELIVERY_ABOVE),
                    "handling_fee": money(settings.HANDLING_FEE),
                    "min_order_value": money(settings.MIN_ORDER_VALUE),
                    # The flat pair: the default tier's numbers, for callers
                    # that only want "what does this store promise?".
                    "promise_minutes": fallback.promise_minutes,
                    "delivery_fee": fallback.fee,
                    # Answered here so the storefront can say "we open at 07:00"
                    # on the cart, rather than accepting a basket, collecting an
                    # address, and only then refusing it. `closed_reason` is the
                    # same string the checkout endpoint would refuse with,
                    # produced by the same method — two implementations of
                    # "are we open?" is how a shop shows one message and enforces
                    # another.
                    "is_open": store.is_open(),
                    "closed_reason": store.closed_reason(),
                    "opens_at": store.opens_at,
                    "closes_at": store.closes_at,
                    "delivery_radius_km": store.delivery_radius_km,
                    "store_latitude": store.store_latitude,
                    "store_longitude": store.store_longitude,
                }
            ).data
        )


# How far back "most ordered" looks.
#
# A window rather than all time, because a product that sold well last winter is
# not what the shop is selling now, and an all-time ranking calcifies: whatever
# led on day one keeps leading, which is a recommendation that stops responding
# to the store. Thirty days matches the console's analytics default, so the
# storefront's idea of popular and the manager's are the same idea.
POPULAR_WINDOW_DAYS = 30


def _sold_since(window_days: int = POPULAR_WINDOW_DAYS):
    """`{category_name_lowercased: units sold}` over the window.

    Grouped through `product__category`, matching
    `analytics.CategoryShareView` — `OrderItem` snapshots the product's name and
    price but not its category, so this reads the aisle a product sits in
    *today*. Moving a product between aisles moves its history with it, which is
    the intended reading of "how is Dairy doing".

    Lower-cased keys for the same reason the tile lookup uses them: "Dairy" and
    "dairy" are one aisle to a shopper, and rows carried over from Supabase use
    both spellings.
    """
    since = timezone.now() - timedelta(days=window_days)

    rows = (
        OrderItem.objects.filter(order__created_at__gte=since)
        .exclude(order__status__in=(Order.CANCELLED, Order.FAILED))
        .values(category=F("product__category"))
        .annotate(units=Coalesce(Sum("quantity"), 0))
    )

    sold: dict[str, int] = {}
    for row in rows:
        name = (row["category"] or "").strip().lower()
        if name:
            sold[name] = sold.get(name, 0) + row["units"]
    return sold


def _by_popularity(products):
    """Order a product queryset by units actually sold, most first.

    **Counted from `OrderItem.quantity`, over orders that became sales.**
    Cancelled and failed orders are excluded for the same reason
    `api/views/analytics.py::counted_orders` excludes them: neither is demand the
    store met, and a run of refused deliveries would otherwise promote the very
    product people keep sending back.

    Products with no sales in the window are not dropped — they sort last, on
    the default ordering. A shop open for a week would otherwise have an almost
    empty "most ordered", and a new product would be invisible until somebody
    bought one, which nobody could, because it was invisible.

    One aggregate, deliberately. `Sum` over the `order_items` join is correct
    precisely because it is the only annotation here — a product with six sales
    produces six joined rows and summing all six is the number wanted. Adding a
    second aggregate over a different relation to the same query is where that
    stops being true and both come back multiplied.
    """
    since = timezone.now() - timedelta(days=POPULAR_WINDOW_DAYS)

    sold = Sum(
        "order_items__quantity",
        filter=Q(order_items__order__created_at__gte=since)
        & ~Q(order_items__order__status__in=(Order.CANCELLED, Order.FAILED)),
    )

    return (
        products.annotate(
            units_sold=Coalesce(sold, 0),
            # A *boolean*, not the stock level. Ordering by `-stock` — which is
            # what the default ordering below does, and what this did first —
            # sorts by how many units are on the shelf, so the best-stocked
            # product wins and popularity never gets a say. What is wanted here
            # is only "can they buy it at all", as a tiebreak-free first key.
            is_available=Q(stock__gt=0),
        )
        # In stock first regardless of popularity: the most-ordered thing in the
        # shop is no use at the top of the page if nobody can buy it today.
        .order_by("-is_available", "-units_sold", "price", "id")
    )


class StoreProductListView(APIView):
    """GET /api/store/products — the sellable catalogue.

    Filtering and paging happen in SQL rather than in the browser. The old
    version returned every product and let React filter the array, which is fine
    for ten rows and indefensible for a real catalogue: it ships the whole
    inventory over the network on first paint.
    """

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str, description="Search name, brand, category, description."),
            OpenApiParameter("category", str, description="Exact category name (case-insensitive)."),
            OpenApiParameter(
                "sort",
                str,
                description=(
                    "`popular` orders by units actually sold in the last "
                    f"{POPULAR_WINDOW_DAYS} days. Anything else is the default: "
                    "in stock first, then cheapest."
                ),
            ),
            OpenApiParameter("limit", int, description=f"Max rows (default {settings.STORE_PAGE_SIZE})."),
            OpenApiParameter("offset", int, description="Rows to skip."),
        ],
        responses=StoreProductSerializer(many=True),
    )
    def get(self, request):
        products = Product.objects.filter(status__iexact=Product.ACTIVE)

        category = (request.query_params.get("category") or "").strip()
        if category and category.lower() != "all":
            products = products.filter(category__iexact=category)

        search = (request.query_params.get("q") or "").strip()
        if search:
            products = products.filter(
                Q(name__icontains=search)
                | Q(brand__icontains=search)
                | Q(category__icontains=search)
                | Q(description__icontains=search)
            )

        if (request.query_params.get("sort") or "").strip().lower() == "popular":
            products = _by_popularity(products)
        else:
            # In-stock items first, then cheapest, so the top of a category is
            # always something the customer can actually buy.
            products = products.order_by("-stock", "price", "id")

        limit, offset = read_page(request)
        return Response(
            StoreProductSerializer(products[offset : offset + limit], many=True).data
        )


class StoreProductDetailView(APIView):
    """GET /api/store/products/{id} — one product, for its own page.

    The catalogue list already carries every field this returns, but a product
    page reached by a shared link or a reload has no list to read from, and
    fetching the whole catalogue to find one row is the sort of thing that works
    at seed scale and falls over at a real one.

    It answers with `StoreProductSerializer`, the same narrow shape as the list.
    The admin's `/api/products/{id}` looks like an easier route to the same data
    and is not: it carries cost price, supplier and shelf location, and it is
    admin-only for exactly that reason.

    An inactive product is a 404 rather than a 200 with a flag. It is not for
    sale, so there is no page for it — and answering differently for "withdrawn"
    than for "never existed" tells a scraper which ids are real.
    """

    @extend_schema(responses=StoreProductSerializer)
    def get(self, request, product_id: int):
        product = (
            Product.objects.filter(status__iexact=Product.ACTIVE, pk=product_id).first()
        )
        if product is None:
            raise NotFound("We could not find that product.")
        return Response(StoreProductSerializer(product).data)


class StoreCategoryListView(APIView):
    """GET /api/store/categories — the tiles in the category rail.

    Built from the products that actually exist rather than from the Category
    table, so a category with nothing sellable in it never renders as an empty
    aisle. `Category` rows supply the tile image, matched by name — that
    name-based relationship is how this schema has always joined the two, since
    `Product.category` is free text rather than a foreign key.
    """

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "sort",
                str,
                description=(
                    "`popular` orders aisles by units sold in the last "
                    f"{POPULAR_WINDOW_DAYS} days. Anything else keeps the "
                    "manager's own sort order."
                ),
            )
        ],
        responses=StoreCategorySerializer(many=True),
    )
    def get(self, request):
        counts = (
            Product.objects.filter(status__iexact=Product.ACTIVE)
            .exclude(category__isnull=True)
            .exclude(category__exact="")
            .values("category")
            .annotate(product_count=Count("id"))
        )

        # Lower-cased keys because "Dairy" and "dairy" are one aisle to a
        # shopper, and rows carried over from Supabase use both.
        meta = {
            category.name.strip().lower(): category
            for category in Category.objects.filter(status__iexact="active")
        }

        tiles = []
        for row in counts:
            name = (row["category"] or "").strip()
            record = meta.get(name.lower())
            tiles.append(
                {
                    "name": name,
                    "image_url": record.image_url if record else None,
                    "product_count": row["product_count"],
                    "_sort": (record.sort_order if record else 0, name.lower()),
                }
            )

        if (request.query_params.get("sort") or "").strip().lower() == "popular":
            # Busiest aisle first, with the manager's order as the tiebreak so a
            # store with no sales yet still gets the arrangement they chose
            # rather than something alphabetical pretending to be a ranking.
            sold = _sold_since()
            tiles.sort(key=lambda tile: (-sold.get(tile["name"].lower(), 0), tile["_sort"]))
        else:
            # The order the manager sorted them into. `sort_order` exists so the
            # shop front is a decision rather than an accident.
            tiles.sort(key=lambda tile: tile["_sort"])

        for tile in tiles:
            tile.pop("_sort")
        return Response(StoreCategorySerializer(tiles, many=True).data)


class BasketQuoteView(APIView):
    """POST /api/store/quote — what this basket would cost.

    Exists so the cart drawer and the final bill are produced by one
    implementation. The obvious alternative — adding up line totals in
    TypeScript — is a second pricing engine that drifts from this one the first
    time a fee changes, and the customer notices at the worst possible moment.

    Takes no locks and writes nothing: quoting is a read.
    """

    throttle_scope = "tracking"

    @extend_schema(request=CheckoutSerializer, responses=BasketQuoteSerializer)
    def post(self, request):
        # `request.data` is whatever the parser produced, and a top-level JSON
        # array parses to a list. Calling .get() on that raises AttributeError,
        # which api/exceptions.py deliberately does not handle -- so a one-line
        # body shape mistake became an unhandled 500 on a public, unauthenticated
        # endpoint. No legitimate client sends that shape; say so as a 400.
        if not isinstance(request.data, dict):
            raise ValidationError("Expected a JSON object with an `items` array.")

        items = request.data.get("items")
        requested_tier = request.data.get("delivery_type")

        # An empty cart is a normal state, not an error — the drawer opens
        # before anything is in it. Answer with a zeroed bill rather than a 400
        # the UI would have to special-case. It still names a tier, so the
        # picker has something to render before the first item goes in.
        if not isinstance(items, list) or not items:
            return Response(self._empty_quote(resolve_tier(requested_tier)))

        # Reuse the checkout serializer's item rules (quantity caps, duplicate
        # merging) and its tier validation, without requiring the customer
        # details, which they have not typed yet at the point the cart drawer
        # opens.
        payload = {
            "items": items,
            # Placeholders that satisfy the required customer fields; none
            # of them are read by the pricing path.
            "customer_name": "quote",
            "customer_phone": "+919000000000",
            "customer_address": "quote placeholder address",
        }
        if requested_tier is not None:
            payload["delivery_type"] = requested_tier

        item_serializer = CheckoutSerializer(data=payload)
        item_serializer.is_valid(raise_exception=True)
        validated = item_serializer.validated_data

        basket, charges = quote(validated["items"], validated.get("delivery_type"))
        return Response(
            BasketQuoteSerializer.build(
                basket.items_total,
                charges,
                basket.unavailable,
                basket.tier,
                basket.lines,
            )
        )

    @staticmethod
    def _empty_quote(tier) -> dict:
        zero = money(0)
        return {
            "items_total": zero,
            "delivery_fee": zero,
            "handling_fee": zero,
            "grand_total": zero,
            "free_delivery_shortfall": money(settings.FREE_DELIVERY_ABOVE),
            "meets_minimum": False,
            "unavailable": [],
            "lines": [],
            "delivery_type": tier.key,
            "promised_minutes": tier.promise_minutes,
        }


class CheckoutView(APIView):
    """POST /api/store/orders — place an order.

    **Public, and it stays public now that customers can have accounts.** Guest
    checkout is the main path, not a fallback: requiring an account here would
    cost a fifth to a third of first-time buyers, and the tracking token and the
    idempotency key were both designed to work without one. A signed-in caller
    is recognised and their order is linked to them; everyone else is served
    exactly as before.

    That makes it the one unauthenticated endpoint in the system that writes
    rows and moves stock, so it is throttled (`checkout` scope) — without a
    limit, a loop empties the catalogue's stock into orders nobody placed.

    **`Idempotency-Key` is read from the header, not the body.** The checkout
    body is the money boundary: it carries product ids and quantities and
    nothing else, and the server reads no price, fee or total from it. Adding a
    field there — even a harmless one — makes that rule something you have to
    check rather than something you can see. A header is also what every other
    payments API uses for this, so a client author already knows the convention.
    """

    throttle_scope = "checkout"

    # Long enough for a UUID with room to spare, short enough that the column
    # cannot be used as free storage. Anything longer is refused rather than
    # truncated: two keys that differ only past the cut would silently collide,
    # and the failure would be one customer receiving another's order.
    MAX_KEY_LENGTH = 64

    @extend_schema(
        request=CheckoutSerializer,
        responses={
            200: OrderTrackingSerializer,
            201: OrderTrackingSerializer,
            409: OrderTrackingSerializer,
        },
    )
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        key = request.headers.get("Idempotency-Key", "").strip()
        if len(key) > self.MAX_KEY_LENGTH:
            raise ValidationError(
                f"Idempotency-Key must be at most {self.MAX_KEY_LENGTH} characters."
            )

        # The account, when there is one — read from the token and never from
        # the body, which is the same rule the rider endpoints follow. The
        # `isinstance` matters: this endpoint is public, so `request.user` may
        # equally be an admin or a rider testing an order, and neither of those
        # should end up owning it.
        customer = request.user if isinstance(request.user, Customer) else None

        try:
            order, created = place_order(
                serializer.validated_data, key, customer=customer
            )
        except BasketUnavailable as exc:
            # 409, not 400: the request was well-formed and was valid when the
            # customer built the basket. What changed is the catalogue.
            return Response(
                {"detail": exc.detail, "unavailable": exc.unavailable},
                status=status.HTTP_409_CONFLICT,
            )

        order = TRACKED_ORDERS.get(pk=order.pk)
        # 200 for a replay. The body is identical either way — same order, same
        # tracking token — so a client that ignores the distinction still works;
        # the status is there for the one that wants to know whether its retry
        # was the request that counted.
        return Response(
            OrderTrackingSerializer(order).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


def get_tracked_order(token: str) -> Order:
    """Resolve a tracking token, or 404.

    The same 404 for a token that never existed and one that is malformed: an
    endpoint that distinguishes them tells an attacker when a guess was closer.
    """
    order = TRACKED_ORDERS.filter(tracking_token=token).first()
    if order is None:
        raise NotFound("We could not find that order.")
    return order


class OrderTrackingView(APIView):
    """GET /api/store/orders/{token} — the customer's live view of one order.

    Polled by an open tracking page, hence its own generous throttle scope
    rather than the default anonymous limit.
    """

    throttle_scope = "tracking"

    @extend_schema(responses=OrderTrackingSerializer)
    def get(self, request, token: str):
        return Response(OrderTrackingSerializer(get_tracked_order(token)).data)


class OrderCancelView(APIView):
    """POST /api/store/orders/{token}/cancel — the customer changes their mind.

    Allowed only while the goods are still in the store. Once a rider has
    collected the order, cancelling it here would put stock back that has
    physically left the building, so the state machine refuses and the customer
    is told to speak to the rider.
    """

    throttle_scope = "tracking"

    @extend_schema(request=CancelOrderSerializer, responses=OrderTrackingSerializer)
    def post(self, request, token: str):
        order = get_tracked_order(token)

        payload = CancelOrderSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        reason = payload.validated_data.get("reason") or "Cancelled by customer"
        cancelled = cancel_order(order, reason)

        return Response(OrderTrackingSerializer(TRACKED_ORDERS.get(pk=cancelled.pk)).data)


class TrackedRiderLocationView(APIView):
    """GET /api/store/orders/{token}/rider-location — where the rider is now.

    **The narrowest window in this API, and the one most worth keeping narrow.**
    It is deliberately a separate route rather than fields on
    `OrderTrackingSerializer`: that serializer's docstring promises "no rider
    identity, no distances", the two-serializer split exists so that publishing
    internal data takes a deliberate edit, and a live position is the most
    sensitive thing the store holds about a member of staff. Keeping it here
    means the whole boundary is one small file somebody can read in a minute.

    Answers `{"rider": null}` in every case where the answer is no — not
    dispatched yet, already delivered, no rider, never reported, reported too
    long ago. They are one situation to the page (nothing to draw) and
    distinguishing them would tell a token holder when a rider's phone went
    dark. See `location.rider_position_for_tracking` for the four gates.

    Its own route also means the page can poll this every few seconds while
    fetching the much larger order payload rarely — which on Aizawl mobile data
    is the difference between a live map and a page that stutters.
    """

    throttle_scope = "tracking"

    @extend_schema(responses=TrackedRiderLocationSerializer)
    def get(self, request, token: str):
        order = get_tracked_order(token)
        position = location_service.rider_position_for_tracking(order)
        if position is None:
            return Response({"rider": None})
        return Response({"rider": TrackedRiderLocationSerializer(position).data})


class CustomerLocationView(APIView):
    """POST /api/store/orders/{token}/location — the customer shares their position.

    **Possession of the tracking token is the whole credential**, which is the
    rule this endpoint already lives under: the same token is enough to read the
    order's name, phone number and address, so being able to attach a position
    to it grants nothing new. There is no account to require and no verification
    to demand — guest checkout is still the main path, and an endpoint that
    needed one would be useless to most customers.

    Opt-in at the page, and declining stays fully supported: everything about
    the delivery works exactly as it does today without this. It never touches
    `Order.customer_latitude` — that is the checkout position the radius check
    and dispatch were decided on, and a later fix taken from a moving car must
    not be able to rewrite where the bag is going.

    **409 once the order has ended.** A conflict with the order's state, the
    same distinction `advance_status` draws — and the point at which
    `advance_status` has already deleted any position that was there.
    """

    throttle_scope = "customer_location"

    @extend_schema(request=CustomerLocationReportSerializer, responses={204: None})
    def post(self, request, token: str):
        order = get_tracked_order(token)

        if order.status in Order.TERMINAL:
            return Response(
                {"detail": "This order has already been completed."},
                status=status.HTTP_409_CONFLICT,
            )

        payload = CustomerLocationReportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        location_service.record_customer_position(
            order,
            latitude=data["latitude"],
            longitude=data["longitude"],
            accuracy_m=data.get("accuracy_m"),
        )
        # 204: the page already knows where it is, and echoing a position back
        # would only invite it to render the server's copy instead of its own.
        return Response(status=status.HTTP_204_NO_CONTENT)
