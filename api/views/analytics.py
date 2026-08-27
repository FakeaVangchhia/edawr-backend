"""Aggregates for the console's dashboard and analytics screens.

Read by both roles. Nothing here is Admin-only: a Manager who runs the store
needs the numbers that describe it.

**Three conventions hold across every endpoint in this file.**

*Everything is aggregated in the database.* `Sum`, `Count`, `Avg` and one
`TruncDate` — no endpoint here loads orders into Python to add them up. The
rider dashboard's stalled-order scan is the counter-example this file
deliberately avoids: it is a per-request loop over every open order, which is
survivable at fifty orders and is not at fifty thousand.

*Everything buckets by `created_at`, in the store's timezone, excluding orders
that never became sales — cancellations and failed deliveries.* One rule,
applied identically everywhere, so two screens can never disagree about what
"this week" contained. `STORE_TIMEZONE` matters here:
Aizawl is UTC+5:30, and grouping by UTC date would file the whole evening rush
under the following day.

*Revenue is booked, not collected.* It sums `grand_total` for every order that
was placed and neither cancelled nor failed at the door. For a cash-on-delivery store the money physically
arrives at the door, so booked and collected differ by whatever is currently out
with a rider; `/api/analytics/delivery` is where the delivered-only view lives.
Saying which one a number is beats picking the cleverer one.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Avg, Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from api.models import Order, OrderItem, Product
from api.permissions import AdminAPIView
from api.pricing import money
from api.serializers import (
    AnalyticsSummarySerializer,
    CashReconciliationSerializer,
    CategoryShareSerializer,
    DeliveryPerformanceSerializer,
    InventoryHealthSerializer,
    RevenuePointSerializer,
    TopProductSerializer,
)

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
ZERO = Decimal("0.00")

# Sum() over no rows is NULL, which would serialise as null and render as an
# empty tile. Coalescing at the database keeps "no sales yet" a number.
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


def store_tz() -> ZoneInfo:
    return ZoneInfo(settings.STORE_TIMEZONE)


def read_date(request, name: str) -> date | None:
    """Parse `?from=YYYY-MM-DD`. Garbage is ignored, never a 500."""
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def read_window(request) -> tuple[date, date]:
    """The requested date range, clamped, inclusive of both ends.

    Defaults to the last 30 days. The cap exists because an unbounded range is a
    full table scan any authenticated caller could ask for repeatedly.
    """
    today = datetime.now(store_tz()).date()
    to_date = read_date(request, "to") or today
    from_date = read_date(request, "from") or (
        to_date - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    )
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    if (to_date - from_date).days > MAX_WINDOW_DAYS:
        from_date = to_date - timedelta(days=MAX_WINDOW_DAYS)
    return from_date, to_date


def span(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    """Inclusive local dates to a half-open UTC datetime range.

    Half-open on purpose: `created_at < end`, where end is midnight *after*
    `to_date`. Using `<=` against a datetime would drop or double-count whatever
    landed in the final second, and comparing against a bare date would make the
    database cast every row and ignore the index.
    """
    tz = store_tz()
    start = datetime.combine(from_date, time.min, tzinfo=tz)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def counted_orders(from_date: date, to_date: date):
    """The one order queryset every figure in this file is built from.

    Excludes both terminal non-sales. `Cancelled` was always excluded; `Failed`
    joined the state machine later and was not added here, which quietly counted
    a refused doorstep delivery as revenue — the order carries a `grand_total`
    and no money ever changed hands. Every figure built on this queryset was
    overstated by exactly the value of the failed drops.
    """
    start, end = span(from_date, to_date)
    return Order.objects.filter(created_at__gte=start, created_at__lt=end).exclude(
        status__in=(Order.CANCELLED, Order.FAILED)
    )


def all_orders(from_date: date, to_date: date):
    """Including cancellations — only the cancellation rate wants this."""
    start, end = span(from_date, to_date)
    return Order.objects.filter(created_at__gte=start, created_at__lt=end)


def metric(value, previous) -> dict:
    return {"value": value, "previous": previous}


class AnalyticsSummaryView(AdminAPIView):
    """GET /api/analytics/summary?from=&to=

    Each headline figure is returned beside the same figure for the window of
    equal length immediately before it. That comparison is computed here rather
    than on the client because "the previous period" has to mean one thing, and
    two clients would define it two ways.
    """

    @extend_schema(responses=AnalyticsSummarySerializer)
    def get(self, request):
        from_date, to_date = read_window(request)
        length = (to_date - from_date).days + 1
        prev_to = from_date - timedelta(days=1)
        prev_from = prev_to - timedelta(days=length - 1)

        def figures(start: date, end: date) -> dict:
            orders = counted_orders(start, end)
            totals = orders.aggregate(
                revenue=Coalesce(Sum("grand_total"), ZERO, output_field=MONEY_FIELD),
                count=Count("id"),
            )
            delivered = orders.filter(status=Order.DELIVERED)
            delivered_count = delivered.count()
            on_time = delivered.filter(was_late=False).count()
            every = all_orders(start, end).count()
            # Counted directly rather than as `every - counted`. That
            # subtraction used to be exact, because cancellation was the only
            # thing `counted_orders` excluded; once failed deliveries were
            # excluded too it would have folded them into a figure labelled
            # "cancellation rate", which is a different thing that happens for
            # different reasons and needs a different response from the store.
            cancelled = (
                all_orders(start, end).filter(status=Order.CANCELLED).count()
            )

            revenue = money(totals["revenue"])
            count = totals["count"]
            return {
                "revenue": revenue,
                "orders": Decimal(count),
                "average_order_value": money(revenue / count) if count else ZERO,
                # A store with nothing delivered yet has no on-time rate at all,
                # not a rate of zero. The console shows the delivered count
                # beside this tile so the placeholder cannot be misread.
                "on_time_rate": (
                    money(Decimal(on_time) * 100 / delivered_count)
                    if delivered_count
                    else ZERO
                ),
                "cancellation_rate": (
                    money(Decimal(cancelled) * 100 / every) if every else ZERO
                ),
            }

        now = figures(from_date, to_date)
        before = figures(prev_from, prev_to)
        return Response(
            {
                **{key: metric(now[key], before[key]) for key in now},
                "from_date": from_date,
                "to_date": to_date,
            }
        )


class RevenueSeriesView(AdminAPIView):
    """GET /api/analytics/revenue?from=&to= — one row per calendar day.

    Days with no orders are filled in with zeroes rather than left out. A line
    chart that silently skips empty days draws a straight line across a dead
    week and reports it as steady trade.
    """

    @extend_schema(responses=RevenuePointSerializer(many=True))
    def get(self, request):
        from_date, to_date = read_window(request)
        rows = (
            counted_orders(from_date, to_date)
            .annotate(day=TruncDate("created_at", tzinfo=store_tz()))
            .values("day")
            .annotate(
                revenue=Coalesce(Sum("grand_total"), ZERO, output_field=MONEY_FIELD),
                orders=Count("id"),
            )
            .order_by("day")
        )
        found = {row["day"]: row for row in rows}

        series = []
        cursor = from_date
        while cursor <= to_date:
            row = found.get(cursor)
            series.append(
                {
                    "date": cursor,
                    "revenue": money(row["revenue"]) if row else ZERO,
                    "orders": row["orders"] if row else 0,
                }
            )
            cursor += timedelta(days=1)
        return Response(series)


class TopProductsView(AdminAPIView):
    """GET /api/analytics/products?from=&to=&limit=&direction=top|bottom

    Aggregated over `OrderItem`, not `Product`, and over its *snapshot* columns:
    `line_total` and `name` are what the customer was actually charged and shown
    at the time. Joining back to the live product would re-price history the next
    time someone edits a price.
    """

    @extend_schema(responses=TopProductSerializer(many=True))
    def get(self, request):
        from_date, to_date = read_window(request)
        try:
            limit = min(max(int(request.query_params.get("limit", 10)), 1), 50)
        except (TypeError, ValueError):
            limit = 10
        ascending = (request.query_params.get("direction") or "top").lower() == "bottom"

        rows = (
            OrderItem.objects.filter(order__in=counted_orders(from_date, to_date))
            .values("product_id", "name")
            .annotate(
                units=Coalesce(Sum("quantity"), 0),
                revenue=Coalesce(Sum("line_total"), ZERO, output_field=MONEY_FIELD),
            )
            .order_by("units" if ascending else "-units", "name")[:limit]
        )
        return Response(
            [
                {
                    "product_id": row["product_id"],
                    "name": row["name"],
                    "units": row["units"],
                    "revenue": money(row["revenue"]),
                }
                for row in rows
            ]
        )


class CategoryShareView(AdminAPIView):
    """GET /api/analytics/categories?from=&to=

    Grouped through `product__category`, because `OrderItem` snapshots the
    product's name and price but not its category. That join reads the category a
    product sits in *today*, so moving a product between categories moves its
    history with it. That is the intended reading — "how is the Dairy aisle
    doing" is a question about the aisle as it stands now.
    """

    @extend_schema(responses=CategoryShareSerializer(many=True))
    def get(self, request):
        from_date, to_date = read_window(request)
        rows = (
            OrderItem.objects.filter(order__in=counted_orders(from_date, to_date))
            .values(category=F("product__category"))
            .annotate(
                units=Coalesce(Sum("quantity"), 0),
                revenue=Coalesce(Sum("line_total"), ZERO, output_field=MONEY_FIELD),
            )
            .order_by("-revenue")
        )
        return Response(
            [
                {
                    "category": row["category"] or "Uncategorised",
                    "units": row["units"],
                    "revenue": money(row["revenue"]),
                }
                for row in rows
            ]
        )


class DeliveryPerformanceView(AdminAPIView):
    """GET /api/analytics/delivery?from=&to=

    The 15-minute promise, measured. `was_late` and `delivered_in_minutes` were
    stamped at the door by `Order.advance_status`, so these figures do not move
    when someone later edits a delivery tier — see the comment on those columns.
    """

    @extend_schema(responses=DeliveryPerformanceSerializer)
    def get(self, request):
        from_date, to_date = read_window(request)
        delivered = counted_orders(from_date, to_date).filter(status=Order.DELIVERED)

        totals = delivered.aggregate(
            delivered=Count("id"),
            late=Count("id", filter=Q(was_late=True)),
            average=Avg("delivered_in_minutes"),
        )
        count = totals["delivered"]

        riders = (
            delivered.filter(delivery_boy__isnull=False)
            .values("delivery_boy_id", "delivery_boy__name")
            .annotate(
                delivered=Count("id"),
                late=Count("id", filter=Q(was_late=True)),
                average=Avg("delivered_in_minutes"),
            )
            .order_by("-delivered")
        )

        return Response(
            {
                "delivered": count,
                "late": totals["late"],
                "on_time_rate": (
                    round((count - totals["late"]) * 100 / count, 1) if count else 0.0
                ),
                "average_minutes": (
                    round(totals["average"], 1)
                    if totals["average"] is not None
                    else None
                ),
                "riders": [
                    {
                        "rider_id": row["delivery_boy_id"],
                        "name": row["delivery_boy__name"],
                        "delivered": row["delivered"],
                        "late": row["late"],
                        "average_minutes": (
                            round(row["average"], 1)
                            if row["average"] is not None
                            else None
                        ),
                    }
                    for row in riders
                ],
            }
        )


def collected_orders(from_date: date, to_date: date):
    """Delivered orders whose cash was taken inside the window.

    **Bucketed by `paid_at`, not `created_at`** — the only endpoint in this file
    that breaks the file-wide rule, and deliberately. Every other figure here
    answers "what did the shop sell on Tuesday", so it groups by when the order
    was placed. This one answers "what should be in the till tonight", and an
    order placed at 23:50 and delivered at 00:05 is money that arrives on
    Wednesday. Reconciling it against Tuesday would leave both days wrong and
    the rider arguing with a report.

    Cancellations and failures cannot appear here regardless: `paid_at` is
    stamped only on the move to Delivered.
    """
    start, end = span(from_date, to_date)
    return (
        Order.objects.filter(paid_at__gte=start, paid_at__lt=end)
        .exclude(amount_collected__isnull=True)
        .select_related("delivery_boy")
    )


class CashReconciliationView(AdminAPIView):
    """GET /api/analytics/cash?from=&to=

    What each rider owes the till, and where it does not add up.

    Cash on delivery is the entire payment model here, and until `paid_at` and
    `amount_collected` existed the only way to answer "how much cash does this
    rider owe?" was to sum `grand_total` over their delivered orders and trust
    it. That is not a reconciliation — it is the expected figure being used as
    if it were the actual one, which is precisely the substitution a cash
    business cannot afford to make.

    So every number here comes in pairs. `expected` is what the orders were
    worth; `collected` is what the rider says they took; `shortfall` is the
    difference, and it is the only figure anyone needs to look at twice.
    """

    @extend_schema(responses=CashReconciliationSerializer)
    def get(self, request):
        from_date, to_date = read_window(request)
        collected = collected_orders(from_date, to_date)

        # `short_orders` counts orders rather than summing money on purpose: one
        # ₹500 shortfall and fifty ₹10 ones are the same total and completely
        # different problems.
        aggregates = {
            "orders": Count("id"),
            "expected": Coalesce(Sum("grand_total"), ZERO, output_field=MONEY_FIELD),
            "collected": Coalesce(
                Sum("amount_collected"), ZERO, output_field=MONEY_FIELD
            ),
            "short_orders": Count(
                "id", filter=Q(amount_collected__lt=F("grand_total"))
            ),
        }

        totals = collected.aggregate(**aggregates)

        riders = (
            collected.values("collected_by_id", "collected_by__name")
            .annotate(**aggregates)
            .order_by("-expected")
        )

        days = (
            collected.annotate(day=TruncDate("paid_at", tzinfo=store_tz()))
            .values("day")
            .annotate(**{k: v for k, v in aggregates.items() if k != "short_orders"})
            .order_by("day")
        )

        return Response(
            {
                **totals,
                "shortfall": money(totals["expected"] - totals["collected"]),
                "riders": [
                    {
                        "rider_id": row["collected_by_id"],
                        "name": row["collected_by__name"],
                        "orders": row["orders"],
                        "expected": row["expected"],
                        "collected": row["collected"],
                        "shortfall": money(row["expected"] - row["collected"]),
                        "short_orders": row["short_orders"],
                    }
                    for row in riders
                ],
                "days": [
                    {
                        "day": row["day"],
                        "orders": row["orders"],
                        "expected": row["expected"],
                        "collected": row["collected"],
                        "shortfall": money(row["expected"] - row["collected"]),
                    }
                    for row in days
                ],
            }
        )


class InventoryHealthView(AdminAPIView):
    """GET /api/analytics/inventory

    Deliberately ignores the date range — stock is a fact about now, not about a
    period, and a "low stock" list filtered to last month would be nonsense.

    `items` is the reorder list: everything at or below its own `reorder_level`,
    emptiest first, because that is the order someone walks the shelves in.
    """

    @extend_schema(responses=InventoryHealthSerializer)
    def get(self, request):
        products = Product.objects.all()
        totals = products.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status=Product.ACTIVE)),
            out=Count("id", filter=Q(stock__lte=0)),
            low=Count("id", filter=Q(stock__gt=0, stock__lte=F("reorder_level"))),
            units=Coalesce(Sum("stock"), 0),
        )
        # Valued at cost: this answers "what is the shelf worth to us", which is
        # a purchasing question. Valuing it at retail would book unearned margin.
        value = products.aggregate(
            value=Coalesce(
                Sum(F("stock") * F("cost_price"), output_field=MONEY_FIELD),
                ZERO,
                output_field=MONEY_FIELD,
            )
        )["value"]

        reorder = products.filter(stock__lte=F("reorder_level")).order_by("stock", "name")[:50]
        return Response(
            {
                "total_products": totals["total"],
                "active_products": totals["active"],
                "out_of_stock": totals["out"],
                "low_stock": totals["low"],
                "stock_units": totals["units"],
                "stock_value": money(value),
                "items": [
                    {
                        "product_id": product.pk,
                        "name": product.name,
                        "units": product.stock,
                        "revenue": money(product.cost_price * product.stock),
                    }
                    for product in reorder
                ],
            }
        )
