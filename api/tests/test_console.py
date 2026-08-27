"""The query surface the console needs, and the two bugs it would have exposed.

Four admin endpoints used to return their whole table with no filter, no search
and no limit. That is survivable while the catalogue is a seed script and stops
being survivable the moment a store builds a real one — so this covers the
filtering, the paging and the `X-Total-Count` header that replaced it.

It also covers the two defects a live-editing dashboard turns from theoretical
into daily: `PUT` writing back stale stock, and a category rename orphaning
every product that named it.
"""

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from api.models import Order, Product, User
from api.tests.base import APITestBase


class ProductQueryTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.milk = self.make_product(name="Amul Taaza Milk", category="Dairy & Bread")
        self.bread = self.make_product(
            name="Britannia Brown Bread", category="Dairy & Bread"
        )
        self.rice = self.make_product(name="Aizawl Local Rice", category="Staples")
        self.as_admin()

    def test_search_matches_name(self):
        response = self.client.get("/api/products?q=milk")
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "Amul Taaza Milk")

    def test_search_matches_category(self):
        response = self.client.get("/api/products?q=staples")
        self.assertEqual(len(response.data), 1)

    def test_category_filter_is_exact(self):
        response = self.client.get("/api/products", {"category": "Dairy & Bread"})
        self.assertEqual(len(response.data), 2)

    def test_out_of_stock_filter(self):
        self.make_product(name="Sold Out", stock=0)
        response = self.client.get("/api/products?stock=out")
        self.assertEqual([row["name"] for row in response.data], ["Sold Out"])

    def test_low_stock_filter_respects_each_products_own_level(self):
        """"Low" is per product: two units of milk is a crisis, two crates of
        imported olives is a year's supply. `reorder_level` is the threshold."""
        low = self.make_product(name="Nearly Out", stock=2)
        low.reorder_level = 5
        low.save(update_fields=["reorder_level"])

        response = self.client.get("/api/products?stock=low")
        self.assertEqual([row["name"] for row in response.data], ["Nearly Out"])

    def test_total_count_header_reports_the_unpaged_total(self):
        """The body stays a bare array — three clients read it that way. The
        total travels in a header, so paging needed no new body shape."""
        response = self.client.get("/api/products?limit=2")
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response["X-Total-Count"], "3")

    def test_offset_pages_through(self):
        first = self.client.get("/api/products?limit=2&offset=0")
        second = self.client.get("/api/products?limit=2&offset=2")
        self.assertEqual(len(first.data), 2)
        self.assertEqual(len(second.data), 1)

    def test_garbage_paging_is_a_default_not_a_500(self):
        response = self.client.get("/api/products?limit=abc&offset=-4")
        self.assertEqual(response.status_code, 200)


class ProductPatchTests(APITestBase):
    """The stock-clobbering fix.

    `PUT` writes every column from a body the client assembled when it opened
    the editor, so a sale landing while the form is open is overwritten on save.
    `PATCH` writes only the columns that were actually sent.
    """

    def test_patch_does_not_write_back_stale_stock(self):
        product = self.make_product(price="50.00", mrp="60.00", stock=20)
        self.as_admin()

        # A checkout takes two units after the editor was opened.
        Product.objects.filter(pk=product.pk).update(stock=18)

        response = self.client.patch(
            f"/api/products/{product.pk}", {"price": "45.00"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        product.refresh_from_db()
        self.assertEqual(product.stock, 18, "PATCH must not resurrect sold stock")
        self.assertEqual(product.price, Decimal("45.00"))

    def test_put_is_gone(self):
        """Removed rather than locked.

        Replace semantics are what PUT means, and that is exactly the problem
        for a row carrying a counter another transaction owns: the client's
        stale `stock` wins whether or not the write is atomic. The client that
        sent it — the storefront's old `/admin` screen — no longer exists.
        """
        product = self.make_product(price="50.00", mrp="60.00", stock=20)
        self.as_admin()
        response = self.client.put(
            f"/api/products/{product.pk}",
            {"name": product.name, "price": "50.00", "mrp": "60.00", "stock": 7},
            format="json",
        )
        self.assertEqual(response.status_code, 405)
        product.refresh_from_db()
        self.assertEqual(product.stock, 20)

    def test_patch_still_validates(self):
        product = self.make_product(price="50.00", mrp="60.00")
        self.as_admin()
        response = self.client.patch(
            f"/api/products/{product.pk}", {"price": "999.00"}, format="json"
        )
        self.assertEqual(response.status_code, 400, "MRP below price must be refused")

    def test_status_must_be_a_real_status(self):
        """`status` was free text, so "actve" silently withdrew a product from
        sale — data loss that looked like a bug rather than a typo."""
        product = self.make_product()
        self.as_admin()
        response = self.client.patch(
            f"/api/products/{product.pk}", {"status": "actve"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_patch_needs_a_console_token(self):
        product = self.make_product()
        self.as_anonymous()
        self.assertEqual(
            self.client.patch(
                f"/api/products/{product.pk}", {"price": "1.00"}, format="json"
            ).status_code,
            401,
        )


class CategoryRenameTests(APITestBase):
    def test_renaming_a_category_carries_its_products(self):
        """`Product.category` is a free-text label, not a foreign key. Without
        this, a rename orphans every product that named the old value: the
        storefront rail loses its image and the category filter returns nothing.
        Nothing errors — it just quietly stops working."""
        category = self.make_category(name="Dairy & Bread")
        product = self.make_product(category="Dairy & Bread")
        other = self.make_product(name="Rice", category="Staples")

        self.as_admin()
        response = self.client.put(
            f"/api/categories/{category.pk}", {"name": "Dairy"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        product.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(product.category, "Dairy")
        self.assertEqual(other.category, "Staples", "only the renamed category moves")

    def test_editing_without_renaming_moves_nothing(self):
        category = self.make_category(name="Dairy & Bread")
        product = self.make_product(category="Dairy & Bread")

        self.as_admin()
        self.client.put(
            f"/api/categories/{category.pk}",
            {"name": "Dairy & Bread", "sort_order": 3},
            format="json",
        )
        product.refresh_from_db()
        self.assertEqual(product.category, "Dairy & Bread")

    def test_category_search(self):
        self.make_category(name="Dairy & Bread")
        self.make_category(name="Staples")
        self.as_admin()
        response = self.client.get("/api/categories?q=dairy")
        self.assertEqual(len(response.data), 1)


class StaffQueryTests(APITestBase):
    def test_role_filter(self):
        self.make_rider(name="Zoramthanga", phone="9000000002")
        User.objects.create(name="A Manager", role=User.MANAGER, phone="+919000000009")
        self.as_admin()

        riders = self.client.get("/api/users?role=delivery")
        self.assertEqual([row["name"] for row in riders.data], ["Zoramthanga"])

        managers = self.client.get("/api/users?role=manager")
        self.assertEqual([row["name"] for row in managers.data], ["A Manager"])

    def test_search_by_phone(self):
        self.make_rider(name="Zoramthanga", phone="9000000002")
        self.as_admin()
        response = self.client.get("/api/users?q=9000000002")
        self.assertEqual(len(response.data), 1)

    def test_active_filter(self):
        self.make_rider(name="Working", phone="9000000002")
        self.make_rider(name="Left", phone="9000000003", active=False)
        self.as_admin()

        self.assertEqual(len(self.client.get("/api/users?active=true").data), 1)
        self.assertEqual(len(self.client.get("/api/users?active=false").data), 1)


class OrderHistoryTests(APITestBase):
    """Until these filters existed the console could only ask for *open* orders,
    so a customer ringing about yesterday could not be looked up at all — the
    order was in the database and unreachable from every screen."""

    def test_search_by_customer_phone(self):
        self.place_order(self.make_product(stock=50))
        self.as_admin()
        response = self.client.get("/api/orders?q=9812345678")
        self.assertEqual(len(response.data), 1)

    def test_search_by_customer_name(self):
        self.place_order(self.make_product(stock=50))
        self.as_admin()
        response = self.client.get("/api/orders", {"q": "Lalrinsangi"})
        self.assertEqual(len(response.data), 1)

    def test_search_by_order_id(self):
        """A bare number is someone reading an id off a slip, so it matches the
        id as well as the text fields rather than needing a second search box."""
        order = self.place_order(self.make_product(stock=50))
        self.as_admin()
        response = self.client.get("/api/orders", {"q": f"#{order.pk}"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], order.pk)

    def test_rider_filter(self):
        rider = self.make_rider()
        order = self.place_order(self.make_product(stock=50))
        self.as_admin()
        self.client.post(
            f"/api/orders/{order.pk}/assign",
            {"delivery_boy_id": rider.pk},
            format="json",
        )
        response = self.client.get(f"/api/orders?rider={rider.pk}")
        self.assertEqual(len(response.data), 1)

    def test_date_range_excludes_older_orders(self):
        self.place_order(self.make_product(stock=50))
        self.as_admin()
        response = self.client.get("/api/orders?from=2020-01-01&to=2020-01-02")
        self.assertEqual(len(response.data), 0)

    def test_date_range_includes_the_final_day(self):
        """The range is inclusive, implemented half-open against the following
        midnight. Comparing `<=` against a datetime drops the last day's orders.

        Note the date is the store's, not UTC. `created_at.date()` would be the
        UTC date, and Aizawl is UTC+5:30 — so for anything ordered after 18:30
        local the two disagree, and this test would look broken while the code
        was right. That is the same off-by-one the analytics buckets exist to
        avoid; see `STORE_TIMEZONE`.
        """
        order = self.place_order(self.make_product(stock=50))
        local = order.created_at.astimezone(ZoneInfo(settings.STORE_TIMEZONE)).date()
        self.as_admin()
        response = self.client.get(
            f"/api/orders?from={local.isoformat()}&to={local.isoformat()}"
        )
        self.assertEqual(len(response.data), 1)

    def test_a_utc_day_is_not_the_stores_day(self):
        """Pins the boundary rather than leaving it implied.

        An order placed at 20:00 in Aizawl is 14:30 UTC the same day, so both
        agree; one placed at 00:30 local is 19:00 UTC the *previous* day. The
        store's evening must file under the store's date, or every daily figure
        is wrong by a third of the busiest hours.
        """
        order = self.place_order(self.make_product(stock=50))
        Order.objects.filter(pk=order.pk).update(
            created_at=datetime(2026, 3, 9, 19, 0, tzinfo=timezone.utc)
        )
        self.as_admin()

        # 19:00 UTC on the 9th is 00:30 on the 10th in Aizawl.
        self.assertEqual(
            len(self.client.get("/api/orders?from=2026-03-10&to=2026-03-10").data), 1
        )
        self.assertEqual(
            len(self.client.get("/api/orders?from=2026-03-09&to=2026-03-09").data), 0
        )

    def test_total_count_header(self):
        self.place_order(self.make_product(stock=50))
        self.as_admin()
        response = self.client.get("/api/orders")
        self.assertEqual(response["X-Total-Count"], "1")
