"""Authentication and authorisation.

The tests that matter most here are the negative ones: what a token *cannot* do.
"""

from api.models import User
from api.security import (
    ADMIN_TOKEN,
    CUSTOMER_TOKEN,
    RIDER_TOKEN,
    create_access_token,
    decode_token,
)
from api.tests.base import ADMIN_PASSWORD, RIDER_PIN, APITestBase
from api.tests.test_checkout import with_throttle_rates


class AdminLoginTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_admin()
        self.as_anonymous()

    def test_correct_credentials_return_a_token(self):
        response = self.client.post(
            "/api/auth/login",
            {"email": self.admin.email, "password": ADMIN_PASSWORD},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["access_token"])

    def test_email_is_matched_case_insensitively(self):
        response = self.client.post(
            "/api/auth/login",
            {"email": self.admin.email.upper(), "password": ADMIN_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_unknown_email_and_wrong_password_are_indistinguishable(self):
        """Otherwise the endpoint is an oracle for which admin accounts exist."""
        unknown = self.client.post(
            "/api/auth/login",
            {"email": "nobody@edawr.test", "password": ADMIN_PASSWORD},
            format="json",
        )
        wrong = self.client.post(
            "/api/auth/login",
            {"email": self.admin.email, "password": "wrong-password"},
            format="json",
        )

        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.data["detail"], wrong.data["detail"])

    def test_deactivated_admin_cannot_sign_in(self):
        self.admin.is_active = False
        self.admin.save()

        response = self.client.post(
            "/api/auth/login",
            {"email": self.admin.email, "password": ADMIN_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    @with_throttle_rates(login="3/min")
    def test_login_is_throttled(self):
        for _ in range(3):
            self.client.post(
                "/api/auth/login", {"email": self.admin.email, "password": "no"}, format="json"
            )

        response = self.client.post(
            "/api/auth/login", {"email": self.admin.email, "password": "no"}, format="json"
        )
        self.assertEqual(response.status_code, 429)


class RiderLoginTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.rider = self.make_rider()
        self.as_anonymous()

    def test_correct_pin_returns_a_token_and_profile(self):
        response = self.client.post(
            "/api/auth/rider/login",
            {"phone": self.rider.phone, "pin": RIDER_PIN},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["access_token"])
        self.assertEqual(response.data["rider"]["id"], self.rider.id)

    def test_response_never_contains_the_pin_or_its_hash(self):
        response = self.client.post(
            "/api/auth/rider/login",
            {"phone": self.rider.phone, "pin": RIDER_PIN},
            format="json",
        )

        body = str(response.data)
        self.assertNotIn("pin_hash", body)
        self.assertNotIn(RIDER_PIN, body.replace(response.data["access_token"], ""))

    def test_wrong_pin_is_rejected(self):
        response = self.client.post(
            "/api/auth/rider/login",
            {"phone": self.rider.phone, "pin": "9999"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_rider_without_a_pin_cannot_sign_in(self):
        self.rider.pin_hash = None
        self.rider.save()

        response = self.client.post(
            "/api/auth/rider/login",
            {"phone": self.rider.phone, "pin": RIDER_PIN},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_deactivated_rider_cannot_sign_in(self):
        self.rider.is_active = False
        self.rider.save()

        response = self.client.post(
            "/api/auth/rider/login",
            {"phone": self.rider.phone, "pin": RIDER_PIN},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_a_managers_phone_is_not_a_rider_login(self):
        manager = User.objects.create(
            name="Manager", role=User.MANAGER, phone="+919000000001"
        )

        response = self.client.post(
            "/api/auth/rider/login",
            {"phone": manager.phone, "pin": RIDER_PIN},
            format="json",
        )
        self.assertEqual(response.status_code, 401)


class TokenSeparationTests(APITestBase):
    """The three token kinds share a secret and are told apart by `typ`."""

    def test_decode_rejects_a_token_of_the_other_type(self):
        rider_token = create_access_token("+919000000002", RIDER_TOKEN)

        self.assertIsNone(decode_token(rider_token, ADMIN_TOKEN))
        self.assertIsNone(decode_token(rider_token, CUSTOMER_TOKEN))
        self.assertEqual(
            decode_token(rider_token, RIDER_TOKEN)["sub"], "+919000000002"
        )

    def test_a_rider_and_a_customer_may_share_a_phone_number(self):
        """The case `typ` exists for, and it is not hypothetical.

        A rider who shops at the shop they deliver for has a row in `users` and
        a row in `customers`, holding one number and two independent passwords.
        Both tokens carry that number in `sub`, signed with the same secret, so
        the claim is the only thing keeping them apart — and getting it wrong
        would hand a customer the rider dashboard.
        """
        shared = "+919000000777"
        rider = self.make_rider(phone=shared)
        customer = self.make_customer(phone=shared)

        self.as_customer(customer)
        self.assertEqual(
            self.client.get(f"/api/delivery/{rider.id}/dashboard").status_code, 403
        )
        self.assertEqual(self.client.get("/api/auth/customer/me").status_code, 200)

        self.as_rider(rider)
        self.assertEqual(
            self.client.get(f"/api/delivery/{rider.id}/dashboard").status_code, 200
        )
        self.assertEqual(self.client.get("/api/auth/customer/me").status_code, 403)

    def test_customer_token_cannot_reach_an_admin_endpoint(self):
        self.as_customer()

        self.assertEqual(self.client.get("/api/products").status_code, 403)

    def test_customer_token_cannot_reach_a_rider_endpoint(self):
        rider = self.make_rider()
        self.as_customer()

        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")

        self.assertEqual(response.status_code, 403)

    def test_staff_tokens_cannot_reach_a_customer_endpoint(self):
        """403 rather than 401 in both directions: we know who they are."""
        self.as_admin()
        self.assertEqual(self.client.get("/api/auth/customer/me").status_code, 403)

        self.as_rider()
        self.assertEqual(self.client.get("/api/auth/customer/me").status_code, 403)

    # 403, not 401, and the distinction is deliberate. DRF answers 401 when it
    # could not identify the caller at all ("who are you?") and 403 when it
    # could but they are not allowed ("I know who you are, no"). A rider holding
    # a perfectly valid rider token is the second case. The clients rely on
    # this: 401 is what makes the web app clear its stored session, and clearing
    # it on 403 would sign an admin out every time they opened a page they
    # happen not to have rights to.
    def test_rider_token_cannot_reach_an_admin_endpoint(self):
        self.as_rider()

        response = self.client.get("/api/products")

        self.assertEqual(response.status_code, 403)

    def test_admin_token_cannot_reach_a_rider_endpoint(self):
        admin = self.make_admin()
        rider = self.make_rider()
        self.as_admin(admin)

        response = self.client.get(f"/api/delivery/{rider.id}/dashboard")

        self.assertEqual(response.status_code, 403)

    def test_deactivating_a_rider_revokes_their_existing_token(self):
        """Tokens live 12 hours; deactivation must not wait that long."""
        rider = self.make_rider()
        self.as_rider(rider)

        self.assertEqual(
            self.client.get(f"/api/delivery/{rider.id}/dashboard").status_code, 200
        )

        rider.is_active = False
        rider.save()

        self.assertEqual(
            self.client.get(f"/api/delivery/{rider.id}/dashboard").status_code, 401
        )

    def test_garbage_token_is_rejected(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")

        response = self.client.get("/api/products")

        self.assertEqual(response.status_code, 401)

    def test_missing_token_is_rejected_on_a_guarded_endpoint(self):
        self.as_anonymous()

        response = self.client.get("/api/products")

        self.assertEqual(response.status_code, 401)


class PublicSurfaceTests(APITestBase):
    """Endpoints that must stay reachable without any credentials."""

    def test_public_endpoints_do_not_require_a_token(self):
        self.as_anonymous()
        for url in [
            "/api/health",
            "/api/health/ready",
            "/api/store/config",
            "/api/store/products",
            "/api/store/categories",
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_report_intake_endpoints_are_public(self):
        """Deliberately so, and this test is the place that says why.

        A crash report is worth having precisely when nobody is signed in, and a
        CSP violation is posted by the browser itself with no way to attach a
        token. Both are throttled under the `reports` scope, both allowlist
        every field they log, and neither touches the database — see
        `views/reports.py` and `test_reports.py`.
        """
        self.as_anonymous()
        for url, body in [
            ("/api/client-errors", {"message": "boom"}),
            ("/api/csp-report", {"csp-report": {"effective-directive": "img-src"}}),
        ]:
            with self.subTest(url=url), self.assertLogs("api.reports", "WARNING"):
                self.assertEqual(
                    self.client.post(url, body, format="json").status_code, 204
                )

    def test_admin_endpoints_all_require_a_token(self):
        self.as_anonymous()
        for method, url in [
            ("get", "/api/products"),
            ("get", "/api/categories"),
            ("get", "/api/users"),
            ("get", "/api/orders"),
            ("get", "/api/delivery/riders"),
            ("post", "/api/uploads/products/image"),
        ]:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 401, url)
