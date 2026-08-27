"""What an uploaded product image is allowed to be, and when it goes away.

Two bugs, both of the kind that only show up long after the change that caused
them: the file's type was taken from a header the client writes, and no upload
was ever deleted by anything.
"""

import tempfile
from pathlib import Path

from django.test import override_settings

from api.models import Category, Product
from api.tests.base import APITestBase

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\0" * 32
JPEG = bytes.fromhex("ffd8ff") + b"\0" * 32
GIF = b"GIF89a" + b"\0" * 32
WEBP = b"RIFF" + b"\0\0\0\0" + b"WEBP" + b"\0" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _upload(content: bytes, name: str, content_type: str):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content, content_type=content_type)


class UploadTypeTests(APITestBase):
    URL = "/api/uploads/products/image"

    def setUp(self):
        super().setUp()
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self.media_root = Path(self._media.name)
        self.as_admin()

    def _post(self, content: bytes, name: str, content_type: str):
        with override_settings(MEDIA_ROOT=str(self.media_root)):
            return self.client.post(
                self.URL, {"file": _upload(content, name, content_type)}, format="multipart"
            )

    def test_a_real_png_is_accepted(self):
        response = self._post(PNG, "photo.png", "image/png")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["image_url"].endswith(".png"))

    def test_every_supported_format_is_recognised_by_its_bytes(self):
        for content, extension in [
            (PNG, ".png"),
            (JPEG, ".jpg"),
            (GIF, ".gif"),
            (WEBP, ".webp"),
        ]:
            with self.subTest(extension=extension):
                # Deliberately mislabelled: the header says nothing useful, and
                # the point is that it is no longer consulted.
                response = self._post(content, "photo.bin", "application/octet-stream")

                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data["image_url"].endswith(extension))

    def test_an_svg_declared_as_a_png_is_refused(self):
        """The header is client-supplied; an SVG can carry script."""
        response = self._post(SVG, "logo.png", "image/png")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.media_root.iterdir()), [])

    def test_the_stored_extension_follows_the_bytes_not_the_name(self):
        response = self._post(PNG, "actually-a-png.jpg", "image/jpeg")

        self.assertTrue(response.data["image_url"].endswith(".png"))

    def test_an_empty_file_is_refused(self):
        self.assertEqual(self._post(b"", "empty.png", "image/png").status_code, 400)


class UploadCleanupTests(APITestBase):
    """Nothing deleted an upload, ever. The working tree had ~250 orphans."""

    def setUp(self):
        super().setUp()
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self.media_root = Path(self._media.name)
        self.as_admin()

    def _write(self, name: str) -> str:
        (self.media_root / name).write_bytes(PNG)
        return f"/uploads/{name}"

    def test_replacing_a_product_image_removes_the_old_file(self):
        old = self._write("old.png")
        new = self._write("new.png")
        product = self.make_product(image_url=old)

        with override_settings(MEDIA_ROOT=str(self.media_root)):
            response = self.client.patch(
                f"/api/products/{product.pk}", {"image_url": new}, format="json"
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse((self.media_root / "old.png").exists())
        self.assertTrue((self.media_root / "new.png").exists())

    def test_editing_something_else_leaves_the_image_alone(self):
        image = self._write("keep.png")
        product = self.make_product(image_url=image)

        with override_settings(MEDIA_ROOT=str(self.media_root)):
            self.client.patch(
                f"/api/products/{product.pk}", {"name": "Renamed"}, format="json"
            )

        self.assertTrue((self.media_root / "keep.png").exists())

    def test_deleting_a_product_removes_its_image(self):
        image = self._write("gone.png")
        product = self.make_product(image_url=image)

        with override_settings(MEDIA_ROOT=str(self.media_root)):
            response = self.client.delete(f"/api/products/{product.pk}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse((self.media_root / "gone.png").exists())

    def test_deleting_a_category_removes_its_tile(self):
        image = self._write("tile.png")
        category = Category.objects.create(name="Snacks", image_url=image)

        with override_settings(MEDIA_ROOT=str(self.media_root)):
            self.client.delete(f"/api/categories/{category.pk}")

        self.assertFalse((self.media_root / "tile.png").exists())

    def test_an_externally_hosted_image_is_left_where_it_is(self):
        """A seeded placeholder on someone else's CDN is not ours to delete."""
        product = self.make_product(image_url="https://cdn.example.com/milk.png")

        with override_settings(MEDIA_ROOT=str(self.media_root)):
            response = self.client.delete(f"/api/products/{product.pk}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_a_traversal_dressed_as_an_image_url_deletes_nothing(self):
        outside = self.media_root.parent / "do-not-touch.txt"
        outside.write_text("important")
        self.addCleanup(outside.unlink, True)
        product = self.make_product(image_url="/uploads/../do-not-touch.txt")

        with override_settings(MEDIA_ROOT=str(self.media_root)):
            self.client.delete(f"/api/products/{product.pk}")

        self.assertTrue(outside.exists())

    def test_an_already_missing_file_is_not_an_error(self):
        product = self.make_product(image_url="/uploads/never-existed.png")

        with override_settings(MEDIA_ROOT=str(self.media_root)):
            response = self.client.delete(f"/api/products/{product.pk}")

        self.assertEqual(response.status_code, 200, response.data)
