"""The JSON error contract, at the edges DRF does not reach.

`api/exceptions.py` guarantees `{"detail": "..."}` for everything DRF raises,
and deliberately declines to handle anything it does not recognise so a real
bug surfaces as a real 500. That left two holes, both only visible with
`DEBUG=False` — which is to say, only in production:

  - a URL matching no pattern fell through to Django's HTML 404;
  - any non-DRF exception fell through to Django's HTML 500.

Both are the one shape the storefront, the console and the rider app cannot
read: all three do `data.detail || 'Something went wrong'`, and all three got an
unparseable body at the exact moment something was already wrong.

These tests run with DEBUG forced off, because with it on Django serves its
debug pages and the handlers are never consulted.
"""

from django.test import override_settings

from api.tests.base import APITestBase


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(APITestBase):
    def test_an_unrouted_url_is_json(self):
        response = self.client.get("/api/prodcts")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"detail": "Not found."})

    def test_a_missing_page_outside_the_api_is_json_too(self):
        """There is no HTML surface here at all — every route is an API route."""
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Not found.")

    def test_a_known_route_with_a_bad_id_still_uses_the_drf_shape(self):
        """DRF's own NotFound already produced the right shape; check it still does.

        The handlers must not shadow it — a 404 raised *inside* a view carries a
        message worth reading ("Product not found."), and replacing it with the
        generic "Not found." would be a regression dressed as consistency.
        """
        response = self.client.get("/api/store/products/999999")

        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.json())

    def test_a_method_a_route_does_not_offer_is_json(self):
        """405 goes through DRF, so this is a check that it still reaches it."""
        self.as_admin()

        response = self.client.get("/api/products/1")

        self.assertEqual(response.status_code, 405)
        self.assertIn("detail", response.json())
