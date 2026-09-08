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


class FakeR2:
    """Enough of a boto3 S3 client for the upload path, and nothing more.

    A stub rather than a mocked-out `api.storage.save`, because the things worth
    asserting here are exactly the arguments that reach the client: the key
    layout, the content type, the cache header. Stubbing one level higher would
    test that the code calls itself.
    """

    def __init__(self, fail=False):
        self.objects = {}
        self.deleted = []
        self.fail = fail

    def upload_fileobj(self, fileobj, Bucket, Key, ExtraArgs=None):  # noqa: N803
        if self.fail:
            raise RuntimeError("bucket unreachable")
        self.objects[Key] = {"bucket": Bucket, "body": fileobj.read(), **(ExtraArgs or {})}

    def delete_object(self, Bucket, Key):  # noqa: N803
        if self.fail:
            raise RuntimeError("bucket unreachable")
        self.deleted.append(Key)
        self.objects.pop(Key, None)


@override_settings(
    UPLOAD_BACKEND="r2",
    R2_BUCKET="edawr-test",
    R2_ENDPOINT_URL="https://example.r2.cloudflarestorage.com",
    R2_ACCESS_KEY_ID="key",
    R2_SECRET_ACCESS_KEY="secret",
    R2_PUBLIC_BASE_URL="https://pub-test.r2.dev",
)
class R2StorageTests(APITestBase):
    """The object-storage backend, against a stub client.

    Nothing here touches the network. `settings.TESTING` also pins
    UPLOAD_BACKEND to "local" globally, so this class's override is the only
    place in the suite where the R2 path runs at all.
    """

    URL = "/api/uploads/products/image"

    def setUp(self):
        super().setUp()
        self.as_admin()
        self.fake = FakeR2()
        # api/storage.py memoises its client in a module global; swap it out and
        # put it back, or the next test inherits this stub.
        self._install(self.fake)

    def _install(self, client):
        from api import storage

        previous = storage._client
        storage._client = client
        self.addCleanup(setattr, storage, "_client", previous)

    def _post(self, content=PNG, name="photo.png", content_type="image/png"):
        return self.client.post(
            self.URL, {"file": _upload(content, name, content_type)}, format="multipart"
        )

    def test_the_response_is_still_a_relative_path(self):
        """The whole migration rests on this: the stored value does not change."""
        response = self._post()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["image_url"].startswith("/uploads/"))
        self.assertNotIn("http", response.data["image_url"])
        self.assertNotIn("r2.dev", response.data["image_url"])

    def test_the_object_key_is_the_stored_path_without_its_slash(self):
        image_url = self._post().data["image_url"]

        self.assertEqual(list(self.fake.objects), [image_url.lstrip("/")])
        self.assertEqual(self.fake.objects[image_url.lstrip("/")]["bucket"], "edawr-test")

    def test_the_content_type_follows_the_bytes_not_the_header(self):
        """A JPEG announced as a PNG is stored as, and served as, a JPEG.

        The same rule the local backend has always enforced through the file
        extension. On R2 it matters more: a wrong ContentType is what the
        browser is given, and octet-stream downloads instead of rendering.
        """
        image_url = self._post(JPEG, "lies.png", "image/png").data["image_url"]

        self.assertTrue(image_url.endswith(".jpg"))
        self.assertEqual(self.fake.objects[image_url.lstrip("/")]["ContentType"], "image/jpeg")

    def test_objects_are_cached_forever(self):
        """Safe only because the filename carries 16 random hex characters."""
        image_url = self._post().data["image_url"]

        self.assertEqual(
            self.fake.objects[image_url.lstrip("/")]["CacheControl"],
            "public, max-age=31536000, immutable",
        )

    def test_the_bytes_arrive_intact(self):
        image_url = self._post(WEBP, "shot.webp", "image/webp").data["image_url"]

        self.assertEqual(self.fake.objects[image_url.lstrip("/")]["body"], WEBP)

    def test_an_svg_is_still_refused_before_anything_is_written(self):
        response = self._post(SVG, "payload.svg", "image/png")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake.objects, {})

    def test_a_store_that_is_down_is_a_502_and_not_a_500(self):
        """Loud, unlike audit and push.

        A manager told nothing here saves a product with no picture and finds
        out from the shop floor.
        """
        self._install(FakeR2(fail=True))

        response = self._post()

        self.assertEqual(response.status_code, 502, response.data)
        self.assertIn("detail", response.data)

    def test_replacing_a_product_image_deletes_the_old_object(self):
        old = self._post(name="old.png").data["image_url"]
        new = self._post(name="new.png").data["image_url"]
        product = self.make_product(image_url=old)

        response = self.client.patch(
            f"/api/products/{product.pk}", {"image_url": new}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.fake.deleted, [old.lstrip("/")])

    def test_deleting_a_category_deletes_its_tile(self):
        image_url = self._post(name="tile.png").data["image_url"]
        category = Category.objects.create(name="Snacks", image_url=image_url)

        self.client.delete(f"/api/categories/{category.pk}")

        self.assertEqual(self.fake.deleted, [image_url.lstrip("/")])

    def test_an_image_on_someone_elses_cdn_is_not_ours_to_delete(self):
        product = self.make_product(image_url="https://cdn.example.com/milk.png")

        response = self.client.delete(f"/api/products/{product.pk}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.fake.deleted, [])

    def test_a_traversal_cannot_reach_a_key_outside_the_prefix(self):
        """The bucket has no directory to be confined to, so this is the guard."""
        product = self.make_product(image_url="/uploads/../../secrets.env")

        self.client.delete(f"/api/products/{product.pk}")

        self.assertEqual(self.fake.deleted, ["uploads/secrets.env"])

    def test_a_store_that_is_down_never_fails_a_delete(self):
        product = self.make_product(image_url="/uploads/whatever.png")
        self._install(FakeR2(fail=True))

        response = self.client.delete(f"/api/products/{product.pk}")

        self.assertEqual(response.status_code, 200, response.data)

    def test_public_url_puts_the_r2_host_in_front(self):
        from api import storage

        self.assertEqual(
            storage.public_url("/uploads/milk-abc.png"),
            "https://pub-test.r2.dev/uploads/milk-abc.png",
        )
        # Not ours, so left exactly as it is.
        self.assertEqual(
            storage.public_url("https://cdn.example.com/milk.png"),
            "https://cdn.example.com/milk.png",
        )
        self.assertIsNone(storage.public_url(None))
