"""The public storefront: catalogue, quote, tracking.

The recurring theme is *what the public response does not contain*.
"""

from decimal import Decimal

from django.test import override_settings

from api.models import Order, Product
from api.tests.base import APITestBase


class CatalogueTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.milk = self.make_product(name="Amul Taaza Milk", price="62.00", mrp="66.00", stock=20)
        self.bread = self.make_product(
            name="Brown Bread", price="45.00", mrp="50.00", stock=5, category="Dairy & Bread"
        )
        self.chips = self.make_product(
            name="Lay's Classic", price="20.00", stock=0, category="Snacks & Munchies"
        )
        self.hidden = self.make_product(
            name="Withdrawn Item", price="10.00", status=Product.INACTIVE
        )
        self.as_anonymous()

    def test_only_active_products_are_listed(self):
        response = self.client.get("/api/store/products")

        names = [row["name"] for row in response.data]
        self.assertNotIn("Withdrawn Item", names)
        self.assertEqual(len(names), 3)

    def test_margin_and_supplier_data_are_never_exposed(self):
        """The single most important assertion in this file."""
        response = self.client.get("/api/store/products")

        for row in response.data:
            for forbidden in [
                "cost_price", "supplier_name", "supplier_phone", "location",
                "reorder_level", "stock", "sku", "barcode",
            ]:
                self.assertNotIn(forbidden, row)

    def test_stock_is_reduced_to_a_boolean_and_a_hint(self):
        response = self.client.get("/api/store/products")
        rows = {row["name"]: row for row in response.data}

        self.assertTrue(rows["Amul Taaza Milk"]["in_stock"])
        self.assertFalse(rows["Amul Taaza Milk"]["low_stock"])
        self.assertTrue(rows["Brown Bread"]["low_stock"])
        self.assertFalse(rows["Lay's Classic"]["in_stock"])

    def test_discount_percent_rounds_down_and_never_lies(self):
        response = self.client.get("/api/store/products")
        rows = {row["name"]: row for row in response.data}

        # 66 -> 62 is 6.06%, shown as 6 rather than 7.
        self.assertEqual(rows["Amul Taaza Milk"]["discount_percent"], 6)
        # No MRP above price means no badge at all.
        self.assertEqual(rows["Lay's Classic"]["discount_percent"], 0)

    def test_in_stock_items_are_listed_first(self):
        response = self.client.get("/api/store/products")

        in_stock = [row["in_stock"] for row in response.data]
        self.assertEqual(in_stock, sorted(in_stock, reverse=True))

    def test_search_matches_the_product_name(self):
        response = self.client.get("/api/store/products?q=brown")
        self.assertEqual([row["name"] for row in response.data], ["Brown Bread"])

    def test_search_also_matches_the_category(self):
        """Searching "bread" should find everything on the bread aisle, not only
        products with "bread" in their name."""
        response = self.client.get("/api/store/products?q=bread")

        names = sorted(row["name"] for row in response.data)
        self.assertEqual(names, ["Amul Taaza Milk", "Brown Bread"])

    def test_category_filter_is_case_insensitive(self):
        response = self.client.get("/api/store/products?category=snacks %26 munchies")
        self.assertEqual([row["name"] for row in response.data], ["Lay's Classic"])

    def test_category_all_returns_everything(self):
        response = self.client.get("/api/store/products?category=all")
        self.assertEqual(len(response.data), 3)

    def test_paging_is_clamped(self):
        with override_settings(STORE_MAX_PAGE_SIZE=2):
            response = self.client.get("/api/store/products?limit=1000")
        self.assertEqual(len(response.data), 2)

    def test_garbage_paging_parameters_do_not_500(self):
        response = self.client.get("/api/store/products?limit=abc&offset=-9")
        self.assertEqual(response.status_code, 200)


class CategoryRailTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.make_category("Dairy & Bread", sort_order=1, image_url="/uploads/dairy.png")
        self.make_category("Snacks & Munchies", sort_order=2)
        self.make_category("Empty Aisle", sort_order=3)
        self.make_product(name="Milk", category="Dairy & Bread")
        self.make_product(name="Chips", category="Snacks & Munchies")
        self.as_anonymous()

    def test_only_categories_with_sellable_products_appear(self):
        response = self.client.get("/api/store/categories")

        names = [row["name"] for row in response.data]
        self.assertEqual(names, ["Dairy & Bread", "Snacks & Munchies"])

    def test_tile_carries_image_and_count(self):
        response = self.client.get("/api/store/categories")

        self.assertEqual(response.data[0]["image_url"], "/uploads/dairy.png")
        self.assertEqual(response.data[0]["product_count"], 1)

    def test_a_category_with_no_matching_row_still_appears(self):
        """Product.category is free text; a product may name an aisle that has
        no Category row, and dropping it would hide sellable stock."""
        self.make_product(name="Orphan", category="Uncategorised Stuff")

        response = self.client.get("/api/store/categories")

        names = [row["name"] for row in response.data]
        self.assertIn("Uncategorised Stuff", names)


class ConfigTests(APITestBase):
    @override_settings(
        DELIVERY_PROMISE_MINUTES=12, DELIVERY_FEE="25.00", FREE_DELIVERY_ABOVE="199.00"
    )
    def test_config_reports_the_live_rules(self):
        self.as_anonymous()

        response = self.client.get("/api/store/config")

        self.assertEqual(response.data["promise_minutes"], 12)
        self.assertMoney(response.data["delivery_fee"], "25.00")
        self.assertMoney(response.data["free_delivery_above"], "199.00")


class QuoteTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="62.00", stock=10)
        self.as_anonymous()

    def test_empty_basket_returns_a_zeroed_bill(self):
        response = self.client.post("/api/store/quote", {"items": []}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertMoney(response.data["grand_total"], "0.00")
        self.assertFalse(response.data["meets_minimum"])

    def test_quote_matches_what_checkout_charges(self):
        """One pricing engine, not two."""
        items = [{"product_id": self.product.id, "quantity": 3}]

        quoted = self.client.post("/api/store/quote", {"items": items}, format="json")
        placed = self.client.post(
            "/api/store/orders",
            self.checkout_payload(self.product, 3),
            format="json",
        )

        self.assertMoney(quoted.data["grand_total"], str(placed.data["grand_total"]))

    def test_quote_reports_the_free_delivery_shortfall(self):
        response = self.client.post(
            "/api/store/quote",
            {"items": [{"product_id": self.product.id, "quantity": 2}]},
            format="json",
        )

        self.assertMoney(response.data["items_total"], "124.00")
        self.assertMoney(response.data["free_delivery_shortfall"], "75.00")

    def test_quote_flags_unavailable_items_without_failing(self):
        # Above stock (10) but within MAX_QUANTITY_PER_ITEM, so it reaches the
        # availability check rather than being rejected as a bad field.
        response = self.client.post(
            "/api/store/quote",
            {"items": [{"product_id": self.product.id, "quantity": 15}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["unavailable"]), 1)

    def test_quote_writes_nothing(self):
        self.client.post(
            "/api/store/quote",
            {"items": [{"product_id": self.product.id, "quantity": 3}]},
            format="json",
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(Order.objects.count(), 0)


class TrackingTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)
        self.order = self.place_order(self.product, 2)
        self.url = f"/api/store/orders/{self.order.tracking_token}"

    def test_token_holder_can_track_without_credentials(self):
        self.as_anonymous()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Order.PLACED)
        self.assertGreater(response.data["minutes_remaining"], 0)

    def test_tracking_is_keyed_on_the_token_not_the_id(self):
        """There is no sequence to walk."""
        response = self.client.get(f"/api/store/orders/{self.order.id}")
        self.assertEqual(response.status_code, 404)

    def test_unknown_token_is_a_404(self):
        response = self.client.get("/api/store/orders/wrong-token-entirely")
        self.assertEqual(response.status_code, 404)

    def test_internal_fields_are_not_exposed(self):
        response = self.client.get(self.url)

        for forbidden in [
            "offered_to_delivery_boy_id", "offered_distance_km", "delivery_boy_id",
            "customer_latitude", "customer_longitude",
        ]:
            self.assertNotIn(forbidden, response.data)

    def test_rider_appears_only_once_dispatched(self):
        rider = self.make_rider(name="Zoramthanga")

        self.assertIsNone(self.client.get(self.url).data["rider"])

        self.order.delivery_boy = rider
        self.order.save()
        self.advance(self.order, Order.PACKING, Order.READY, Order.DISPATCHED)

        rider_data = self.client.get(self.url).data["rider"]
        self.assertEqual(rider_data["name"], "Zoramthanga")
        self.assertEqual(rider_data["phone"], rider.phone)
        # Home coordinates are the rider's business, not the customer's.
        self.assertNotIn("base_latitude", rider_data)

    def test_can_cancel_flag_tracks_the_state_machine(self):
        self.assertTrue(self.client.get(self.url).data["can_cancel"])

        self.advance(self.order, Order.PACKING, Order.READY, Order.DISPATCHED)

        self.assertFalse(self.client.get(self.url).data["can_cancel"])

    def test_timeline_timestamps_are_visible(self):
        self.advance(self.order, Order.PACKING, Order.READY)

        response = self.client.get(self.url)

        self.assertIsNotNone(response.data["packed_at"])
        self.assertIsNone(response.data["dispatched_at"])


class MoneySerialisationTests(APITestBase):
    def test_money_is_a_json_number_not_a_string(self):
        """The React and React Native apps do arithmetic on these."""
        product = self.make_product(price="62.50", stock=5)
        self.as_anonymous()

        response = self.client.get("/api/store/products")

        self.assertIsInstance(response.data[0]["price"], (float, int, Decimal))
        self.assertNotIsInstance(response.data[0]["price"], str)
