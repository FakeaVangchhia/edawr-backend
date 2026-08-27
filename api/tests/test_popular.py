"""`?sort=popular` on the public catalogue and the public aisle list.

Most ordered, honestly.

The home page wanted to feature "most ordered" items, and the storefront had no
way to know what those were: `/api/analytics/products` is admin-only, and the
public catalogue could be sorted by price and nothing else. The tempting fix was
to label the cheapest items "most ordered" and move on, which is the mistake
`HomePage`'s own docstring warns about — the prototype's "Trending Near You" row
with no trend data behind it.

So this is real: units actually sold, over orders that actually became sales.

What it deliberately does **not** do is tell the customer the number. The count
drives the ordering and never reaches the wire. `StoreProductSerializer` already
withholds cost price, supplier and exact stock; how many units the shop moves in
a month is the same class of fact, and a competitor reading it learns the
store's throughput.
"""

from datetime import timedelta

from django.utils import timezone

from api.models import Order, OrderItem
from api.tests.base import APITestBase


class PopularSortTests(APITestBase):
    URL = "/api/store/products?sort=popular&limit=50"

    def setUp(self):
        super().setUp()
        self.as_anonymous()
        self.quiet = self.make_product(name="Quiet Shelf Item", price="20.00", stock=50)
        self.steady = self.make_product(name="Steady Seller", price="30.00", stock=50)
        self.star = self.make_product(name="Everyone Buys This", price="40.00", stock=50)

    def _sell(self, product, quantity: int, *, status=Order.DELIVERED, days_ago=0):
        """One order carrying `quantity` of `product`, placed `days_ago` back."""
        order = Order.objects.create(
            customer_name="Buyer",
            customer_phone="+919812345678",
            customer_address="House 42, Chanmari, Aizawl",
            status=status,
            created_at=timezone.now() - timedelta(days=days_ago),
        )
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=quantity,
            name=product.name,
            price=product.price,
            mrp=product.mrp,
            line_total=product.price * quantity,
        )
        return order

    def _names(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        return [row["name"] for row in response.data]

    def test_most_units_sold_comes_first(self):
        self._sell(self.star, 9)
        self._sell(self.steady, 3)

        names = self._names()

        self.assertEqual(names[0], "Everyone Buys This")
        self.assertEqual(names[1], "Steady Seller")

    def test_units_are_summed_across_orders_not_counted_as_orders(self):
        """Three orders of one unit must lose to one order of nine."""
        for _ in range(3):
            self._sell(self.steady, 1)
        self._sell(self.star, 9)

        self.assertEqual(self._names()[0], "Everyone Buys This")

    def test_a_product_nobody_bought_still_appears(self):
        """It sorts last, it does not vanish.

        A shop open a week would otherwise have an almost empty "most ordered",
        and a new product would stay invisible until someone bought one — which
        nobody could, because it was invisible.
        """
        self._sell(self.star, 5)

        names = self._names()

        # Present, and behind the one that actually sold. Not asserted to be
        # *last*: `steady` has no sales here either, and the two unsold products
        # are then ordered by price, which is the documented tiebreak.
        self.assertIn("Quiet Shelf Item", names)
        self.assertLess(names.index("Everyone Buys This"), names.index("Quiet Shelf Item"))

    def test_cancelled_orders_do_not_make_a_product_popular(self):
        self._sell(self.steady, 2)
        self._sell(self.star, 50, status=Order.CANCELLED)

        self.assertEqual(self._names()[0], "Steady Seller")

    def test_failed_deliveries_do_not_make_a_product_popular(self):
        """The product people keep sending back is not the one to feature."""
        self._sell(self.steady, 2)
        self._sell(self.star, 50, status=Order.FAILED)

        self.assertEqual(self._names()[0], "Steady Seller")

    def test_sales_outside_the_window_do_not_count(self):
        """Last winter's hit is not what the shop is selling now."""
        self._sell(self.star, 40, days_ago=90)
        self._sell(self.steady, 2)

        self.assertEqual(self._names()[0], "Steady Seller")

    def test_an_out_of_stock_favourite_does_not_lead_the_page(self):
        """The most-ordered thing is no use on top if nobody can buy it today."""
        self.star.stock = 0
        self.star.save(update_fields=["stock"])
        self._sell(self.star, 40)
        self._sell(self.steady, 1)

        names = self._names()

        self.assertEqual(names[0], "Steady Seller")
        self.assertEqual(names[-1], "Everyone Buys This")

    def test_the_sales_count_never_reaches_the_customer(self):
        """Ordering only. Throughput is the store's business, not the public's."""
        self._sell(self.star, 7)

        row = self.client.get(self.URL).data[0]

        for leaked in ("units_sold", "units", "sold", "order_count"):
            self.assertNotIn(leaked, row)

    def test_an_inactive_product_is_still_excluded(self):
        """The sort must not widen what the public catalogue exposes."""
        hidden = self.make_product(name="Withdrawn", stock=10, status="inactive")
        self._sell(hidden, 99)

        self.assertNotIn("Withdrawn", self._names())

    def test_the_default_sort_is_unchanged(self):
        """Anything other than `popular` keeps in-stock-then-cheapest."""
        self._sell(self.star, 40)

        response = self.client.get("/api/store/products?limit=50")

        names = [row["name"] for row in response.data]
        self.assertLess(names.index("Quiet Shelf Item"), names.index("Everyone Buys This"))

    def test_an_unrecognised_sort_falls_back_rather_than_failing(self):
        response = self.client.get("/api/store/products?sort=banana&limit=5")

        self.assertEqual(response.status_code, 200)

    def test_popularity_composes_with_the_category_filter(self):
        self.star.category = "Snacks"
        self.star.save(update_fields=["category"])
        self._sell(self.star, 9)

        response = self.client.get("/api/store/products?sort=popular&category=Snacks")

        self.assertEqual([row["name"] for row in response.data], ["Everyone Buys This"])


class PopularCategoryTests(APITestBase):
    """`GET /api/store/categories?sort=popular` — the busiest aisles.

    Feeds the two image cards in the storefront hero. Same window and same
    exclusions as the product sort, because "popular" meaning two different
    things on one page is how a shop front starts contradicting itself.
    """

    URL = "/api/store/categories?sort=popular"

    def setUp(self):
        super().setUp()
        self.as_anonymous()
        self.make_category("Snacks", sort_order=2)
        self.make_category("Dairy", sort_order=1)
        self.snack = self.make_product(name="Crisps", category="Snacks", stock=50)
        self.milk = self.make_product(name="Milk", category="Dairy", stock=50)

    def _sell(self, product, quantity: int, *, status=Order.DELIVERED, days_ago=0):
        order = Order.objects.create(
            customer_name="Buyer",
            customer_phone="+919812345678",
            customer_address="House 42, Chanmari, Aizawl",
            status=status,
            created_at=timezone.now() - timedelta(days=days_ago),
        )
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=quantity,
            name=product.name,
            price=product.price,
            mrp=product.mrp,
            line_total=product.price * quantity,
        )

    def _names(self, url=None):
        response = self.client.get(url or self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        return [row["name"] for row in response.data]

    def test_the_busiest_aisle_comes_first(self):
        self._sell(self.snack, 9)
        self._sell(self.milk, 2)

        self.assertEqual(self._names()[0], "Snacks")

    def test_units_are_summed_across_the_aisle(self):
        """An aisle wins on its total, not on its best single product."""
        self._sell(self.milk, 8)
        self._sell(self.snack, 3)

        self.assertEqual(self._names()[0], "Dairy")

    def test_cancelled_and_failed_orders_do_not_promote_an_aisle(self):
        self._sell(self.milk, 2)
        self._sell(self.snack, 40, status=Order.CANCELLED)
        self._sell(self.snack, 40, status=Order.FAILED)

        self.assertEqual(self._names()[0], "Dairy")

    def test_sales_outside_the_window_do_not_count(self):
        self._sell(self.snack, 40, days_ago=90)
        self._sell(self.milk, 1)

        self.assertEqual(self._names()[0], "Dairy")

    def test_with_no_sales_the_managers_order_is_kept(self):
        """Not alphabetical, and not random — `sort_order` is a decision."""
        self.assertEqual(self._names(), ["Dairy", "Snacks"])

    def test_the_managers_order_breaks_ties(self):
        self._sell(self.snack, 4)
        self._sell(self.milk, 4)

        self.assertEqual(self._names(), ["Dairy", "Snacks"])

    def test_the_default_order_is_unchanged(self):
        self._sell(self.snack, 40)

        self.assertEqual(self._names("/api/store/categories"), ["Dairy", "Snacks"])

    def test_an_aisle_with_nothing_sellable_never_appears(self):
        """The endpoint builds tiles from products, and popularity must not
        widen that — an empty aisle is not a place to send a customer."""
        self.make_category("Ghost", sort_order=0)

        self.assertNotIn("Ghost", self._names())
