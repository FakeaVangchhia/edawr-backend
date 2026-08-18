"""Admin and Manager are different, and the difference is enforced server-side.

The console hides Admin-only navigation from a Manager, but that is cosmetic —
`lib/guard.ts` decides what to *draw*. These tests cover the half that decides
what is *allowed*, which is the only half an attacker interacts with.

Three properties, and the third is the one that is easy to get wrong:

1. A Manager can run the store. Every operational endpoint answers them.
2. A Manager cannot reach `/api/admins` or `/api/audit`, and is told **403**,
   not 401 — they are perfectly well identified, they simply may not.
3. A role change takes effect on the **next request**, with no new token. The
   role is read from the row on every request rather than carried in the JWT, so
   a demotion is a revocation and not a twelve-hour wait.
"""

from api.models import AdminUser
from api.tests.base import ADMIN_EMAIL, APITestBase

# Everything a Manager is expected to be able to reach. Written as a list rather
# than looped over `urls.py` on purpose: this is the specification, and it should
# have to be edited deliberately when the permission surface changes.
MANAGER_ALLOWED = [
    ("get", "/api/products"),
    ("get", "/api/categories"),
    ("get", "/api/users"),
    ("get", "/api/orders"),
    ("get", "/api/delivery/riders"),
    ("get", "/api/analytics/summary"),
    ("get", "/api/analytics/revenue"),
    ("get", "/api/analytics/products"),
    ("get", "/api/analytics/categories"),
    ("get", "/api/analytics/delivery"),
    ("get", "/api/analytics/inventory"),
]

ADMIN_ONLY = [
    ("get", "/api/admins"),
    ("get", "/api/audit"),
]


class ManagerAccessTests(APITestBase):
    def test_manager_reaches_every_operational_endpoint(self):
        self.as_manager()
        for method, url in MANAGER_ALLOWED:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 200, url)

    def test_manager_can_create_a_product(self):
        self.as_manager()
        response = self.client.post(
            "/api/products",
            {"name": "Manager's Milk", "price": "50.00", "mrp": "60.00",
             "stock": 5, "category": "Dairy & Bread"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_manager_can_manage_riders(self):
        """Staff management is a Manager's job; *console account* management is
        not. The two are different tables and the split is deliberate."""
        self.as_manager()
        response = self.client.post(
            "/api/users",
            {"name": "New Rider", "role": "delivery", "phone": "9812345670",
             "pin": "5926"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)


class AdminOnlyTests(APITestBase):
    def test_manager_is_refused_the_admin_only_surface(self):
        self.as_manager()
        for method, url in ADMIN_ONLY:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 403, url)

    def test_refusal_is_403_not_401(self):
        """401 is what makes a client clear its stored session.

        A Manager who lands on an Admin-only screen must be told no and stay
        signed in. Returning 401 here would sign them out mid-shift — the same
        confusion that logs riders out of the mobile app on a business-rule 403.
        """
        self.as_manager()
        response = self.client.get("/api/admins")
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(response.status_code, 401)

    def test_admin_reaches_the_admin_only_surface(self):
        self.as_admin()
        for method, url in ADMIN_ONLY:
            with self.subTest(url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 200)

    def test_anonymous_still_gets_401_on_admin_only_routes(self):
        """Unauthenticated is a different answer from unauthorised."""
        self.as_anonymous()
        for method, url in ADMIN_ONLY:
            with self.subTest(url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 401)

    def test_rider_token_gets_403_on_admin_only_routes(self):
        self.as_rider()
        self.assertEqual(self.client.get("/api/admins").status_code, 403)


class RoleIsReadFromTheRowTests(APITestBase):
    def test_demotion_takes_effect_without_a_new_token(self):
        """The whole reason the role is not a JWT claim.

        The token minted for this admin is still valid and unexpired after the
        demotion; if the role travelled in it, this second request would
        succeed and the demotion would not take hold for twelve hours.
        """
        admin = self.as_admin()
        self.make_admin(email="second@edawr.test")  # so the last-Admin guard is not the cause
        self.assertEqual(self.client.get("/api/admins").status_code, 200)

        AdminUser.objects.filter(pk=admin.pk).update(role=AdminUser.MANAGER)

        self.assertEqual(self.client.get("/api/admins").status_code, 403)

    def test_promotion_also_takes_effect_immediately(self):
        manager = self.as_manager()
        self.assertEqual(self.client.get("/api/audit").status_code, 403)

        AdminUser.objects.filter(pk=manager.pk).update(role=AdminUser.ADMIN)

        self.assertEqual(self.client.get("/api/audit").status_code, 200)

    def test_deactivation_revokes_access_immediately(self):
        admin = self.as_admin()
        self.assertEqual(self.client.get("/api/products").status_code, 200)

        AdminUser.objects.filter(pk=admin.pk).update(is_active=False)

        # 401, not 403: the authentication class filters on is_active, so the
        # row no longer resolves and the request is anonymous again.
        self.assertEqual(self.client.get("/api/products").status_code, 401)


class LoginPayloadTests(APITestBase):
    def test_login_returns_the_role(self):
        self.make_admin()
        self.as_anonymous()
        response = self.client.post(
            "/api/auth/login",
            {"email": ADMIN_EMAIL, "password": "admin-password-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["role"], AdminUser.ADMIN)
        self.assertEqual(response.data["email"], ADMIN_EMAIL)
        # Kept for the storefront's existing console, which reads it.
        self.assertEqual(response.data["username"], ADMIN_EMAIL)

    def test_login_stamps_last_login(self):
        admin = self.make_admin()
        self.assertIsNone(admin.last_login_at)
        self.as_anonymous()
        self.client.post(
            "/api/auth/login",
            {"email": ADMIN_EMAIL, "password": "admin-password-1"},
            format="json",
        )
        admin.refresh_from_db()
        self.assertIsNotNone(admin.last_login_at)


class AccountGuardTests(APITestBase):
    """The three refusals that stop the console being locked out of itself."""

    def test_cannot_change_own_role(self):
        admin = self.as_admin()
        self.make_admin(email="second@edawr.test")  # not the last Admin
        response = self.client.put(
            f"/api/admins/{admin.pk}", {"role": AdminUser.MANAGER}, format="json"
        )
        self.assertEqual(response.status_code, 409, response.data)
        admin.refresh_from_db()
        self.assertEqual(admin.role, AdminUser.ADMIN)

    def test_cannot_deactivate_self(self):
        admin = self.as_admin()
        self.make_admin(email="second@edawr.test")
        response = self.client.put(
            f"/api/admins/{admin.pk}", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, 409, response.data)

    def test_cannot_demote_the_last_admin(self):
        """The guard that matters. Demoting the only Admin is a coherent request
        that would leave nobody able to undo it."""
        admin = self.as_admin()
        other = self.make_manager()

        response = self.client.put(
            f"/api/admins/{other.pk}", {"role": AdminUser.MANAGER}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        # Now demote the only remaining Admin, from a different account.
        self.as_admin(other)  # still a Manager — cannot reach the endpoint at all
        self.assertEqual(
            self.client.put(f"/api/admins/{admin.pk}", {"role": "manager"},
                            format="json").status_code,
            403,
        )

    def test_last_admin_cannot_be_deactivated_by_another_admin(self):
        first = self.as_admin()
        second = self.make_admin(email="second@edawr.test")

        # Two Admins: deactivating one is fine.
        response = self.client.put(
            f"/api/admins/{second.pk}", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        # One Admin left, and it is the caller — refused as self-deactivation.
        response = self.client.put(
            f"/api/admins/{first.pk}", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, 409)

    def test_delete_deactivates_rather_than_removes(self):
        self.as_admin()
        victim = self.make_manager()
        response = self.client.delete(f"/api/admins/{victim.pk}")
        self.assertEqual(response.status_code, 200, response.data)
        victim.refresh_from_db()
        self.assertFalse(victim.is_active)

    def test_creating_an_account_requires_a_password(self):
        self.as_admin()
        response = self.client.post(
            "/api/admins",
            {"email": "nopass@edawr.test", "role": "manager"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_created_account_can_sign_in(self):
        """End to end: an Admin mints a Manager, and that Manager can log in and
        is told their role. This is the replacement for `manage.py seed` being
        the only way an account ever came into existence."""
        self.as_admin()
        response = self.client.post(
            "/api/admins",
            {"email": "New.Manager@Edawr.test", "name": "Zovi",
             "role": "manager", "password": "a-good-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        # Stored lowercased, or login could never find it.
        self.assertEqual(response.data["email"], "new.manager@edawr.test")
        self.assertNotIn("password", response.data)
        self.assertNotIn("password_hash", response.data)

        self.as_anonymous()
        login = self.client.post(
            "/api/auth/login",
            {"email": "new.manager@edawr.test", "password": "a-good-password"},
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.data)
        self.assertEqual(login.data["role"], "manager")
