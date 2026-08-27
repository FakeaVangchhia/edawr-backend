"""Signing out actually signs you out, and a session cannot renew forever.

Before this, "sign out" was a client deleting its own copy of a token that kept
working for another twelve hours in anybody else's hands — and `/api/auth/me`
minted a fresh twelve hours on every call, so a token copied out of the
console's localStorage was a permanent credential renewable on the same cadence
as the owner's.

Two mechanisms fix that and both are tested here: a `ver` claim compared against
the account row, and an `ait` claim that survives refresh so the session has an
age rather than only an expiry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import override_settings

from api.models import AdminUser, User
from api.security import ADMIN_TOKEN, RIDER_TOKEN, create_access_token
from api.tests.base import ADMIN_PASSWORD, RIDER_PIN, APITestBase


class AdminLogoutTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.admin = self.as_admin()

    def test_logout_retires_the_token_that_called_it(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)

        # Same credentials, same client, and now unrecognised.
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)

    def test_a_retired_token_is_401_and_not_403(self):
        """The distinction the whole codebase turns on.

        401 is what makes a client discard its stored session and show the login
        screen; 403 leaves it signed in on a page it merely lacks rights for. A
        signed-out token is the first case — we no longer know who is calling —
        and answering 403 would leave the console stuck in a loop of failing
        requests with no way back to the login form.
        """
        self.client.post("/api/auth/logout")

        response = self.client.get("/api/products")
        self.assertEqual(response.status_code, 401)

    def test_logout_retires_every_device_not_just_this_one(self):
        """The documented trade-off, asserted so it cannot regress silently."""
        other_device = create_access_token(
            self.admin.email, ADMIN_TOKEN, version=self.admin.token_version
        )

        self.client.post("/api/auth/logout")

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_device}")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_logging_out_twice_is_not_an_error(self):
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        # The second call has no valid credential left, so it cannot reach the
        # view — 401 rather than 204, and the client clears its session either
        # way. What matters is that nothing 500s.
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 401)

    def test_signing_in_again_works_after_a_logout(self):
        self.client.post("/api/auth/logout")
        self.as_anonymous()

        response = self.client.post(
            "/api/auth/login",
            {"email": self.admin.email, "password": ADMIN_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}"
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_logout_needs_a_token(self):
        self.as_anonymous()
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 401)


class RiderLogoutTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.rider = self.as_rider()

    def test_logout_retires_the_riders_token(self):
        self.assertEqual(self.client.get("/api/auth/rider/me").status_code, 200)

        self.assertEqual(self.client.post("/api/auth/rider/logout").status_code, 204)

        self.assertEqual(self.client.get("/api/auth/rider/me").status_code, 401)

    def test_a_rider_cannot_sign_out_of_the_console(self):
        """The two logout endpoints are as separate as the two token kinds.

        403, not 401: this is a valid rider token, so we know exactly who is
        calling and the answer is that they may not.
        """
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 403)

    def test_signing_in_again_works_after_a_logout(self):
        self.client.post("/api/auth/rider/logout")
        self.as_anonymous()

        response = self.client.post(
            "/api/auth/rider/login",
            {"phone": self.rider.phone, "pin": RIDER_PIN},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}"
        )
        self.assertEqual(self.client.get("/api/auth/rider/me").status_code, 200)


class TokenVersionTests(APITestBase):
    """The claim itself, below the endpoints that move it."""

    def test_a_stale_version_is_rejected(self):
        admin = self.make_admin()
        stale = create_access_token(admin.email, ADMIN_TOKEN, version=0)

        AdminUser.objects.filter(pk=admin.pk).update(token_version=1)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {stale}")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_a_version_from_the_future_is_rejected_too(self):
        """Not a real scenario, but the comparison must be equality rather than
        `<`. A `>=` would let anyone who could mint a token pick a version high
        enough to survive every future sign-out."""
        admin = self.make_admin()
        forged = create_access_token(admin.email, ADMIN_TOKEN, version=99)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {forged}")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    @staticmethod
    def legacy_token(subject: str, **claims) -> str:
        """A token in the shape `create_access_token` minted before `ver` and
        `ait` existed: sub, typ, iat, exp and nothing else."""
        import jwt
        from django.conf import settings

        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "sub": subject,
                "typ": ADMIN_TOKEN,
                "iat": now,
                "exp": now + timedelta(hours=1),
                **claims,
            },
            settings.JWT_SECRET,
            algorithm=settings.JWT_ALGORITHM,
        )

    def test_a_token_with_no_version_claim_still_works(self):
        """Deliberate compatibility: the deploy that introduces `ver` must not
        eject every rider and manager mid-shift. A missing claim reads as
        generation 0, which matches any account that has not signed out since,
        and `session_started` falls back to `iat` so the session still has an
        age."""
        admin = self.make_admin()

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.legacy_token(admin.email)}"
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_a_malformed_claim_is_rejected_rather_than_raised(self):
        """`decode_token` must answer "not mine" for anything it cannot read.

        A token carrying `"iat": null` reaches `int(payload["iat"])` inside
        PyJWT's claim validation and raises a bare `TypeError` — not a
        `PyJWTError` — which used to escape the authentication class and become
        a 500 on every request carrying it. Signature verification runs first so
        this needs the signing key to reach, but an authentication path that can
        raise an unhandled exception is one bad claim away from an outage, and
        401 is the honest answer either way.
        """
        admin = self.make_admin()
        undated = self.legacy_token(admin.email, iat=None)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {undated}")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_a_rider_version_is_independent_of_an_admin_one(self):
        """The two tables have overlapping primary keys and separate counters;
        bumping one must not affect the other."""
        rider = self.make_rider()
        admin = self.make_admin()
        User.objects.filter(pk=rider.pk).update(token_version=5)

        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {create_access_token(admin.email, ADMIN_TOKEN, version=0)}"
            )
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)


class CredentialResetTests(APITestBase):
    """A reset that leaves the old sessions alive is not a reset."""

    def test_resetting_an_admin_password_signs_that_account_out(self):
        owner = self.as_admin()
        victim = self.make_admin(email="other@edawr.test")
        victim_token = create_access_token(
            victim.email, ADMIN_TOKEN, version=victim.token_version
        )

        response = self.client.put(
            f"/api/admins/{victim.pk}",
            {"password": "a-new-password-9"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {victim_token}")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

        # And the admin who did the resetting is untouched.
        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {create_access_token(owner.email, ADMIN_TOKEN, version=owner.token_version)}"
            )
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_an_edit_that_is_not_a_password_reset_leaves_the_session_alone(self):
        """Renaming an account must not sign that person out."""
        self.as_admin()
        other = self.make_admin(email="other@edawr.test")
        token = create_access_token(
            other.email, ADMIN_TOKEN, version=other.token_version
        )

        self.client.put(f"/api/admins/{other.pk}", {"name": "Renamed"}, format="json")

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_resetting_a_rider_pin_signs_that_rider_out(self):
        self.as_admin()
        rider = self.make_rider()
        token = create_access_token(
            rider.phone, RIDER_TOKEN, version=rider.token_version
        )

        response = self.client.put(
            f"/api/users/{rider.pk}", {"pin": "9182"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.client.get("/api/auth/rider/me").status_code, 401)


class SessionLifetimeTests(APITestBase):
    """`/me` renews a token. Something has to stop it renewing forever."""

    @staticmethod
    def token_for(subject: str, *, token_type: str, hours_ago: int) -> str:
        started = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return create_access_token(
            subject, token_type, version=0, session_started_at=started
        )

    def test_a_session_inside_the_window_renews(self):
        admin = self.make_admin()
        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {self.token_for(admin.email, token_type=ADMIN_TOKEN, hours_ago=100)}"
            )
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_a_session_past_the_window_is_refused(self):
        admin = self.make_admin()
        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {self.token_for(admin.email, token_type=ADMIN_TOKEN, hours_ago=200)}"
            )
        )
        response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 401)
        self.assertIn("sign in", response.data["detail"].lower())

    def test_the_rider_endpoint_has_the_same_ceiling(self):
        rider = self.make_rider()
        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {self.token_for(rider.phone, token_type=RIDER_TOKEN, hours_ago=200)}"
            )
        )
        self.assertEqual(self.client.get("/api/auth/rider/me").status_code, 401)

    def test_a_refresh_carries_the_original_session_start_forward(self):
        """The claim that makes the ceiling mean anything.

        If `/me` reset the session clock on every call — which is what happens
        if `ait` is minted fresh instead of copied — the ceiling would never be
        reached by anyone who kept using the app, which is precisely the person
        holding a stolen token.
        """
        from api.security import decode_token

        admin = self.make_admin()
        old = self.token_for(admin.email, token_type=ADMIN_TOKEN, hours_ago=100)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {old}")

        renewed = self.client.get("/api/auth/me").data["access_token"]

        # Note the renewed token can be byte-identical to the old one when both
        # are minted inside the same second — `iat` is second-granularity and
        # every other claim is carried forward. That is harmless (they are
        # equally valid) and is why this asserts on the claim rather than on the
        # string differing.
        self.assertEqual(
            decode_token(renewed, ADMIN_TOKEN)["ait"],
            decode_token(old, ADMIN_TOKEN)["ait"],
        )

    @override_settings(SESSION_MAX_HOURS=1)
    def test_the_ceiling_is_configurable(self):
        admin = self.make_admin()
        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {self.token_for(admin.email, token_type=ADMIN_TOKEN, hours_ago=2)}"
            )
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_the_ceiling_does_not_block_ordinary_requests(self):
        """Only renewal is capped. A token still inside its twelve hours keeps
        working right up to its own expiry — cutting a rider off mid-delivery
        because their *session* is a week old would be worse than the risk."""
        admin = self.make_admin()
        self.client.credentials(
            HTTP_AUTHORIZATION=(
                f"Bearer {self.token_for(admin.email, token_type=ADMIN_TOKEN, hours_ago=200)}"
            )
        )
        self.assertEqual(self.client.get("/api/products").status_code, 200)
