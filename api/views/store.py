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

from django.conf import settings
from django.db.models import Count, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from api.checkout import BasketUnavailable, cancel_order, place_order, quote
from api.models import Category, Order, Product
from api.paging import read_page
from api.pricing import default_tier, delivery_tiers, money, resolve_tier
from api.serializers import (
    BasketQuoteSerializer,
    CancelOrderSerializer,
    CheckoutSerializer,
    OrderTrackingSerializer,
    StoreCategorySerializer,
    StoreConfigSerializer,
    StoreProductSerializer,
)

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
                }
            ).data
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

    @extend_schema(responses=StoreCategorySerializer(many=True))
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

        tiles.sort(key=lambda tile: tile.pop("_sort"))
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
                basket.items_total, charges, basket.unavailable, basket.tier
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
            "delivery_type": tier.key,
            "promised_minutes": tier.promise_minutes,
        }


class CheckoutView(APIView):
    """POST /api/store/orders — place an order.

    Public: a customer has no account. That makes it the one unauthenticated
    endpoint in the system that writes rows and moves stock, so it is throttled
    (`checkout` scope) — without a limit, a loop empties the catalogue's stock
    into orders nobody placed.
    """

    throttle_scope = "checkout"

    @extend_schema(
        request=CheckoutSerializer,
        responses={201: OrderTrackingSerializer, 409: OrderTrackingSerializer},
    )
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            order = place_order(serializer.validated_data)
        except BasketUnavailable as exc:
            # 409, not 400: the request was well-formed and was valid when the
            # customer built the basket. What changed is the catalogue.
            return Response(
                {"detail": exc.detail, "unavailable": exc.unavailable},
                status=status.HTTP_409_CONFLICT,
            )

        order = TRACKED_ORDERS.get(pk=order.pk)
        return Response(
            OrderTrackingSerializer(order).data, status=status.HTTP_201_CREATED
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
