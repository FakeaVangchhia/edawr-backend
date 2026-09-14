"""The home-page banners: what the console may do and what the storefront sees.

The two halves are tested against each other on purpose. The console writes
through `PromoSerializer`, the storefront reads through `Promo.live()` and
`StorePromoSerializer`, and the interesting failures are the ones between them:
a hidden banner that still shows, an expired one that lingers, a status column
that leaks.
"""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from api.models import AuditLog, Promo
from api.tests.base import APITestBase

ADMIN_URL = "/api/promos"
PUBLIC_URL = "/api/store/promos"


def make_promo(title="Fresh this week", **overrides) -> Promo:
    return Promo.objects.create(title=title, **overrides)


class PromoAdminTests(APITestBase):
    def test_manager_can_create_and_it_is_audited(self):
        self.as_manager()
        response = self.client.post(
            ADMIN_URL,
            {"title": "Diwali sweets", "subtitle": "In stock now", "link": "/category/sweets"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "active")
        self.assertEqual(response.data["sort_order"], 0)
        self.assertIsNone(response.data["starts_at"])
        entry = AuditLog.objects.get(entity="promo", action=AuditLog.CREATE)
        self.assertEqual(entry.entity_id, response.data["id"])

    def test_anonymous_is_401_and_rider_is_403(self):
        self.assertEqual(self.client.get(ADMIN_URL).status_code, 401)
        self.as_rider()
        self.assertEqual(self.client.get(ADMIN_URL).status_code, 403)

    def test_link_is_a_path_or_an_allowlisted_destination(self):
        self.as_admin()
        for good in [
            "/category/dairy",
            "https://example.com/sale",
            "http://example.com",
            "https://wa.me/919876543210?text=Hi",
            "tel:+919876543210",
            "mailto:hello@example.com",
            "  /offers  ",
        ]:
            response = self.client.post(ADMIN_URL, {"title": "x", "link": good}, format="json")
            self.assertEqual(response.status_code, 201, good)
            self.assertEqual(response.data["link"], good.strip())
        for bad in [
            "//example.com",
            "products",
            "example.com",
            "javascript:alert(1)",
            "data:text/html,hi",
            "ftp://example.com",
            "https://",
            "https://localhost",
            "tel:",
            "mailto:",
            # The browser reads a backslash as a slash and strips tabs and
            # newlines before parsing, so each of these resolves off-site.
            "/\\evil.com",
            "/\t/evil.com",
            "/x\n//evil.com",
            "/ evil.com",
            "https://example.com/\\x",
        ]:
            response = self.client.post(ADMIN_URL, {"title": "x", "link": bad}, format="json")
            self.assertEqual(response.status_code, 400, bad)
        response = self.client.post(ADMIN_URL, {"title": "x", "link": ""}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["link"])

    def test_window_must_be_ordered(self):
        self.as_admin()
        now = timezone.now()
        response = self.client.post(
            ADMIN_URL,
            {"title": "x", "starts_at": now, "ends_at": now - timedelta(hours=1)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("ends_at", str(response.data))

    def test_put_replaces_and_records_the_diff(self):
        self.as_admin()
        promo = make_promo(sort_order=3, link="/offers")
        response = self.client.put(
            f"{ADMIN_URL}/{promo.pk}", {"title": "Renamed", "status": "inactive"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        promo.refresh_from_db()
        self.assertEqual(promo.title, "Renamed")
        self.assertEqual(promo.status, "inactive")
        # Omitted on a PUT, so reset — PUT replaces.
        self.assertEqual(promo.sort_order, 0)
        self.assertIsNone(promo.link)
        entry = AuditLog.objects.get(entity="promo", action=AuditLog.UPDATE)
        self.assertIn("title", entry.changes)
        self.assertIn("status", entry.changes)

    def test_replacing_the_image_deletes_the_old_file(self):
        self.as_admin()
        promo = make_promo(image_url="/uploads/old.png")
        with patch("api.views.promos.delete_stored_image") as delete:
            self.client.put(
                f"{ADMIN_URL}/{promo.pk}",
                {"title": promo.title, "image_url": "/uploads/new.png"},
                format="json",
            )
        delete.assert_called_once_with("/uploads/old.png")

    def test_delete_removes_row_and_image(self):
        self.as_manager()
        promo = make_promo(image_url="/uploads/banner.png")
        with patch("api.views.promos.delete_stored_image") as delete:
            response = self.client.delete(f"{ADMIN_URL}/{promo.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Promo.objects.filter(pk=promo.pk).exists())
        delete.assert_called_once_with("/uploads/banner.png")
        self.assertTrue(AuditLog.objects.filter(entity="promo", action=AuditLog.DELETE).exists())

    def test_missing_promo_is_404(self):
        self.as_admin()
        self.assertEqual(self.client.delete(f"{ADMIN_URL}/999").status_code, 404)

    def test_list_pages_and_filters_by_status(self):
        self.as_admin()
        make_promo("A")
        make_promo("B", status="inactive")
        response = self.client.get(ADMIN_URL)
        self.assertEqual(response["X-Total-Count"], "2")
        response = self.client.get(ADMIN_URL, {"status": "inactive"})
        self.assertEqual([row["title"] for row in response.data], ["B"])


class PromoStorefrontTests(APITestBase):
    def test_only_live_banners_in_rail_order(self):
        now = timezone.now()
        make_promo("Second", sort_order=2)
        make_promo("First", sort_order=1)
        make_promo("Hidden", status="inactive")
        make_promo("Expired", ends_at=now - timedelta(minutes=1))
        make_promo("Not yet", starts_at=now + timedelta(days=1))
        make_promo(
            "Running",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=1),
            sort_order=3,
        )

        response = self.client.get(PUBLIC_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["title"] for row in response.data], ["First", "Second", "Running"])

    def test_public_payload_carries_no_status_or_dates(self):
        make_promo("A", subtitle="b", image_url="/uploads/a.png", link="/offers")
        row = self.client.get(PUBLIC_URL).data[0]
        self.assertEqual(set(row), {"id", "title", "subtitle", "image_url", "link"})

    def test_public_is_anonymous(self):
        self.as_anonymous()
        self.assertEqual(self.client.get(PUBLIC_URL).status_code, 200)
