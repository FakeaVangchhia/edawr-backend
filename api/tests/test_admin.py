"""Admin CRUD: products, categories, staff, uploads."""

import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile

from api.models import Category, Order, Product, User
from api.tests.base import APITestBase

# A one-pixel PNG. Small enough to inline, real enough that Django's uploader
# treats it as a file rather than an empty part.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ProductAdminTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def test_create_and_read_back(self):
        response = self.client.post(
            "/api/products",
            {"name": "Amul Butter", "price": "58.00", "mrp": "62.00", "stock": 20},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertMoney(response.data["price"], "58.00")
        self.assertEqual(response.data["discount_percent"], 6)

    def test_admin_view_includes_cost_price(self):
        self.make_product()

        response = self.client.get("/api/products")

        self.assertIn("cost_price", response.data[0])

    def test_mrp_below_price_is_rejected(self):
        """It would render as a negative discount on the storefront."""
        response = self.client.post(
            "/api/products",
            {"name": "Backwards", "price": "100.00", "mrp": "50.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_optional_text_fields_accept_empty_strings(self):
        """The product editor submits "" for every input it leaves untouched."""
        response = self.client.post(
            "/api/products",
            {"name": "Sparse", "sku": "", "brand": "", "description": ""},
            format="json",
        )

        self.assertEqual(response.status_code, 201)

    def test_put_replaces_rather_than_patches(self):
        product = self.make_product()

        self.client.put(
            f"/api/products/{product.id}", {"name": "Renamed"}, format="json"
        )

        product.refresh_from_db()
        self.assertEqual(product.name, "Renamed")
        self.assertEqual(product.price, Decimal("0.00"))

    def test_deleting_a_product_with_order_history_is_a_409(self):
        product = self.make_product(stock=10)
        self.place_order(product, 1)
        self.as_admin()

        response = self.client.delete(f"/api/products/{product.id}")

        self.assertEqual(response.status_code, 409)
        self.assertTrue(Product.objects.filter(pk=product.id).exists())

    def test_deleting_an_unused_product_succeeds(self):
        product = self.make_product()

        response = self.client.delete(f"/api/products/{product.id}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(pk=product.id).exists())

    def test_non_numeric_id_never_reaches_the_view(self):
        self.assertEqual(self.client.get("/api/products/abc").status_code, 404)


class CategoryAdminTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def test_duplicate_names_are_rejected(self):
        self.make_category("Dairy & Bread")

        response = self.client.post(
            "/api/categories", {"name": "Dairy & Bread"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_a_category_cannot_be_its_own_parent(self):
        category = self.make_category("Dairy")

        response = self.client.put(
            f"/api/categories/{category.id}",
            {"name": "Dairy", "parent_id": category.id},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_a_parent_loop_is_rejected(self):
        top = self.make_category("Top")
        child = Category.objects.create(name="Child", parent=top)

        response = self.client.put(
            f"/api/categories/{top.id}",
            {"name": "Top", "parent_id": child.id},
            format="json",
        )

        self.assertEqual(response.status_code, 400)


class StaffAdminTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def test_create_a_rider_with_a_pin(self):
        response = self.client.post(
            "/api/users",
            {"name": "New Rider", "role": "delivery", "phone": "9812345670", "pin": "4813"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("pin", response.data)
        self.assertNotIn("pin_hash", response.data)

        rider = User.objects.get(pk=response.data["id"])
        self.assertTrue(rider.pin_hash)
        self.assertEqual(rider.phone, "+919812345670")

    def test_weak_pins_are_rejected(self):
        for pin in ["1234", "0000", "1111", "abcd"]:
            with self.subTest(pin=pin):
                response = self.client.post(
                    "/api/users",
                    {"name": "R", "role": "delivery", "phone": "9812345671", "pin": pin},
                    format="json",
                )
                self.assertEqual(response.status_code, 400)

    def test_bad_role_is_rejected(self):
        response = self.client.post(
            "/api/users",
            {"name": "R", "role": "wizard", "phone": "9812345672"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_phone_is_rejected(self):
        self.make_rider(phone="+919812345673")

        response = self.client.post(
            "/api/users",
            {"name": "R", "role": "delivery", "phone": "9812345673"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_pin_can_be_rotated_without_a_shell(self):
        """A forgotten PIN used to need `manage.py shell`."""
        rider = self.make_rider(phone="+919812345674")
        before = rider.pin_hash

        response = self.client.put(
            f"/api/users/{rider.id}", {"pin": "5927"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        rider.refresh_from_db()
        self.assertNotEqual(rider.pin_hash, before)

    def test_updating_a_rider_without_a_pin_does_not_clear_it(self):
        """`pin` is write-only, so a read-modify-write round trip cannot send
        it — under replace semantics that would lock the rider out."""
        rider = self.make_rider(phone="+919812345675")
        before = rider.pin_hash

        self.client.put(f"/api/users/{rider.id}", {"name": "Renamed"}, format="json")

        rider.refresh_from_db()
        self.assertEqual(rider.pin_hash, before)
        self.assertEqual(rider.name, "Renamed")

    def test_rider_with_history_is_deactivated_not_deleted(self):
        product = self.make_product(stock=5)
        rider = self.make_rider(phone="+919812345676")
        order = self.place_order(product, 1)
        order.delivery_boy = rider
        order.save()
        self.as_admin()

        response = self.client.delete(f"/api/users/{rider.id}")

        self.assertEqual(response.status_code, 200)
        rider.refresh_from_db()
        self.assertFalse(rider.is_active)
        self.assertTrue(User.objects.filter(pk=rider.id).exists())
        # Order history must still say who delivered it.
        order.refresh_from_db()
        self.assertEqual(order.delivery_boy_id, rider.id)

    def test_rider_with_no_history_is_deleted(self):
        rider = self.make_rider(phone="+919812345677")

        self.client.delete(f"/api/users/{rider.id}")

        self.assertFalse(User.objects.filter(pk=rider.id).exists())


class UploadTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def upload(self, name: str, content: bytes, content_type: str):
        return self.client.post(
            "/api/uploads/products/image",
            {"file": SimpleUploadedFile(name, content, content_type=content_type)},
            format="multipart",
        )

    def test_uploads_an_image(self):
        response = self.upload("photo.png", PNG_BYTES, "image/png")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["image_url"].startswith("/uploads/"))

    def test_stored_path_is_relative_so_the_host_is_not_baked_in(self):
        response = self.upload("photo.png", PNG_BYTES, "image/png")

        self.assertNotIn("http", response.data["image_url"])

    def test_path_traversal_in_the_filename_is_neutralised(self):
        response = self.upload("../../etc/passwd.png", PNG_BYTES, "image/png")

        url = response.data["image_url"]
        self.assertNotIn("..", url)
        self.assertNotIn("/etc/", url)
        self.assertTrue(url.startswith("/uploads/"))

    def test_two_uploads_of_the_same_name_do_not_collide(self):
        first = self.upload("photo.png", PNG_BYTES, "image/png")
        second = self.upload("photo.png", PNG_BYTES, "image/png")

        self.assertNotEqual(first.data["image_url"], second.data["image_url"])

    def test_non_image_content_type_is_rejected(self):
        response = self.upload("script.svg", b"<svg onload=alert(1)>", "image/svg+xml")

        self.assertEqual(response.status_code, 400)

    def test_empty_file_is_rejected(self):
        response = self.upload("empty.png", b"", "image/png")

        self.assertEqual(response.status_code, 400)

    def test_missing_file_is_rejected(self):
        response = self.client.post("/api/uploads/products/image", {}, format="multipart")

        self.assertEqual(response.status_code, 400)


class HealthTests(APITestBase):
    def test_liveness_touches_nothing(self):
        self.as_anonymous()

        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")

    def test_readiness_reports_the_database(self):
        self.as_anonymous()

        response = self.client.get("/api/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["checks"]["database"], "ok")
