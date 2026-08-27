"""Customer sign-up, sign-in, sign-out and password change.

Follows `test_auth.py`: heavy on the negative cases, because the interesting
assertions here are all about what the API refuses and what it declines to
reveal while refusing.
"""

from api.models import Customer, Order
from api.security import CUSTOMER_TOKEN, create_access_token, verify_password
from api.tests.base import CUSTOMER_PASSWORD, CUSTOMER_PHONE, APITestBase

SIGNUP = "/api/auth/customer/signup"
LOGIN = "/api/auth/customer/login"
ME = "/api/auth/customer/me"
LOGOUT = "/api/auth/customer/logout"
PASSWORD = "/api/auth/customer/password"

GOOD_PASSWORD = "basket-of-milk-7"


class CustomerSignupTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.as_anonymous()

    def test_signup_returns_a_token_that_works(self):
        response = self.client.post(
            SIGNUP,
            {"phone": "9812345678", "password": GOOD_PASSWORD, "name": "Lalringa"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["customer"]["phone"], "+919812345678")
        self.assertEqual(response.data["customer"]["name"], "Lalringa")
        # Nothing verifies a number yet, so a fresh account is unverified and
        # the client is told so rather than having to assume.
        self.assertFalse(response.data["customer"]["phone_verified"])

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}"
        )
        self.assertEqual(self.client.get(ME).status_code, 200)

    def test_the_password_hash_never_leaves_the_server(self):
        response = self.client.post(
            SIGNUP, {"phone": "9812345678", "password": GOOD_PASSWORD}, format="json"
        )

        self.assertNotIn("password", response.data["customer"])
        self.assertNotIn("password_hash", response.data["customer"])
        self.assertNotIn("token_version", response.data["customer"])

    def test_a_taken_number_is_refused_with_409(self):
        self.make_customer(phone="+919812345678")

        response = self.client.post(
            SIGNUP, {"phone": "9812345678", "password": GOOD_PASSWORD}, format="json"
        )

        self.assertEqual(response.status_code, 409)

    def test_a_taken_number_does_not_change_the_existing_password(self):
        """Sign-up is not a back door to a password reset.

        Without this, anyone could type a stranger's number into the sign-up
        form and take the account — which is the whole reason the duplicate is
        an error rather than an update.
        """
        existing = self.make_customer(phone="+919812345678")

        self.client.post(
            SIGNUP, {"phone": "9812345678", "password": "a-different-one-9"}, format="json"
        )

        existing.refresh_from_db()
        self.assertTrue(verify_password(CUSTOMER_PASSWORD, existing.password_hash))

    def test_two_spellings_of_one_number_are_one_account(self):
        """`+91 98123 45678` and `09812345678` are the same person.

        They normalise to one string on the way in, so the second sign-up
        collides. Without it a customer could hold two accounts and see half
        their orders in each.
        """
        first = self.client.post(
            SIGNUP, {"phone": "9812345678", "password": GOOD_PASSWORD}, format="json"
        )
        self.assertEqual(first.status_code, 201)

        for spelling in ("+919812345678", "09812345678", "98123 45678"):
            response = self.client.post(
                SIGNUP, {"phone": spelling, "password": GOOD_PASSWORD}, format="json"
            )
            self.assertEqual(response.status_code, 409, spelling)

        self.assertEqual(Customer.objects.count(), 1)

    def test_a_bad_number_is_a_400(self):
        response = self.client.post(
            SIGNUP, {"phone": "12345", "password": GOOD_PASSWORD}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Customer.objects.count(), 0)


class PasswordStrengthTests(APITestBase):
    """What `validate_password_strength` refuses, through the real endpoint."""

    def setUp(self):
        super().setUp()
        self.as_anonymous()

    def _signup(self, password: str, phone: str = "9812345678"):
        return self.client.post(
            SIGNUP, {"phone": phone, "password": password}, format="json"
        )

    def test_too_short_is_refused(self):
        self.assertEqual(self._signup("milk12").status_code, 400)

    def test_all_digits_is_refused(self):
        """The rule that matters most on an account keyed by a phone number.

        Asked for a password, someone identified by ten digits reaches for ten
        digits — and a numeric password on a numeric identity is close to no
        password at all.
        """
        self.assertEqual(self._signup("48130625").status_code, 400)

    def test_the_customers_own_number_is_refused(self):
        """UserAttributeSimilarityValidator, reading the unsaved instance."""
        self.assertEqual(self._signup("9812345678").status_code, 400)

    def test_a_common_password_is_refused(self):
        self.assertEqual(self._signup("password").status_code, 400)

    def test_nothing_is_created_when_the_password_is_refused(self):
        self._signup("password")
        self.assertEqual(Customer.objects.count(), 0)


class CustomerLoginTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.customer = self.make_customer()
        self.as_anonymous()

    def test_correct_credentials_return_a_token(self):
        response = self.client.post(
            LOGIN, {"phone": CUSTOMER_PHONE, "password": CUSTOMER_PASSWORD}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["customer"]["id"], self.customer.pk)

    def test_login_stamps_last_login_at(self):
        self.assertIsNone(self.customer.last_login_at)

        self.client.post(
            LOGIN, {"phone": CUSTOMER_PHONE, "password": CUSTOMER_PASSWORD}, format="json"
        )

        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.last_login_at)

    def test_every_failure_is_indistinguishable(self):
        """An unknown number, a wrong password and a deactivated account.

        One body and one status for all three, so the endpoint cannot be used
        to discover who shops here.
        """
        self.make_customer(phone="+919000000909", active=False)

        attempts = [
            {"phone": "+919000000404", "password": CUSTOMER_PASSWORD},   # unknown
            {"phone": CUSTOMER_PHONE, "password": "wrong-password-11"},  # wrong
            {"phone": "+919000000909", "password": CUSTOMER_PASSWORD},   # inactive
        ]

        bodies = set()
        for body in attempts:
            response = self.client.post(LOGIN, body, format="json")
            self.assertEqual(response.status_code, 401, body)
            bodies.add(response.data["detail"])

        self.assertEqual(len(bodies), 1)


class CustomerSessionTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.customer = self.as_customer()

    def test_me_refreshes_the_token(self):
        """The bug this test exists for is silent and total.

        `renewable_session` picks the token type from the type of
        `request.user`. Without a Customer branch it falls through to the admin
        default, `decode_token` refuses the mismatched `typ`, and every
        customer is signed out on their first refresh — not on a bad token, on
        every token, every time.
        """
        response = self.client.get(ME)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["access_token"])

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}"
        )
        self.assertEqual(self.client.get(ME).status_code, 200)

    def test_patch_changes_the_name(self):
        response = self.client.patch(ME, {"name": "Zorami"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Zorami")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Zorami")

    def test_logout_retires_the_token(self):
        self.assertEqual(self.client.post(LOGOUT).status_code, 204)

        # Same credentials, now refusing to authenticate: 401, so the client
        # knows to clear its stored session rather than to show a "forbidden".
        self.assertEqual(self.client.get(ME).status_code, 401)

    def test_a_deactivated_account_loses_a_live_token_immediately(self):
        Customer.objects.filter(pk=self.customer.pk).update(is_active=False)

        self.assertEqual(self.client.get(ME).status_code, 401)

    def test_a_stale_version_claim_is_refused(self):
        stale = create_access_token(self.customer.phone, CUSTOMER_TOKEN, version=99)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {stale}")

        self.assertEqual(self.client.get(ME).status_code, 401)

    def test_no_credentials_is_401_not_403(self):
        """401 is what makes the storefront clear its stored session."""
        self.as_anonymous()

        self.assertEqual(self.client.get(ME).status_code, 401)


class CustomerPasswordChangeTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.customer = self.as_customer()

    def test_change_returns_a_working_token(self):
        response = self.client.post(
            PASSWORD,
            {"current_password": CUSTOMER_PASSWORD, "new_password": "new-basket-42"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)

        # The returned token must carry the *bumped* version. If the view hands
        # back a token minted from the un-refreshed `F()` expression, this is
        # the assertion that catches it.
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}"
        )
        self.assertEqual(self.client.get(ME).status_code, 200)

    def test_change_signs_out_every_other_device(self):
        other_device = create_access_token(
            self.customer.phone, CUSTOMER_TOKEN, version=self.customer.token_version
        )

        self.client.post(
            PASSWORD,
            {"current_password": CUSTOMER_PASSWORD, "new_password": "new-basket-42"},
            format="json",
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other_device}")
        self.assertEqual(self.client.get(ME).status_code, 401)

    def test_the_new_password_is_the_one_that_works(self):
        self.client.post(
            PASSWORD,
            {"current_password": CUSTOMER_PASSWORD, "new_password": "new-basket-42"},
            format="json",
        )
        self.as_anonymous()

        stale = self.client.post(
            LOGIN, {"phone": CUSTOMER_PHONE, "password": CUSTOMER_PASSWORD}, format="json"
        )
        self.assertEqual(stale.status_code, 401)

        fresh = self.client.post(
            LOGIN, {"phone": CUSTOMER_PHONE, "password": "new-basket-42"}, format="json"
        )
        self.assertEqual(fresh.status_code, 200)

    def test_the_current_password_must_be_right(self):
        """A borrowed phone with an open session is not a takeover."""
        response = self.client.post(
            PASSWORD,
            {"current_password": "not-the-password", "new_password": "new-basket-42"},
            format="json",
        )

        self.assertEqual(response.status_code, 401)
        self.customer.refresh_from_db()
        self.assertTrue(verify_password(CUSTOMER_PASSWORD, self.customer.password_hash))

    def test_a_weak_new_password_is_refused(self):
        response = self.client.post(
            PASSWORD,
            {"current_password": CUSTOMER_PASSWORD, "new_password": "12345678"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)


class SignupClaimTests(APITestBase):
    """Signing up at checkout, carrying the order just placed."""

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=10)

    def test_the_named_order_is_linked_to_the_new_account(self):
        """Otherwise the account is created and shows an empty history.

        The tracking token is the evidence — the same possession that already
        lets the public tracking endpoint show a name and an address.
        """
        order = self.place_order(self.product, 1)

        response = self.client.post(
            SIGNUP,
            {
                "phone": "9812345678",
                "password": GOOD_PASSWORD,
                "claim_token": order.tracking_token,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        order.refresh_from_db()
        self.assertEqual(order.customer_id, response.data["customer"]["id"])

    def test_an_order_already_owned_cannot_be_taken(self):
        order = self.place_order(self.product, 1)
        owner = self.make_customer(phone="+919000000501")
        Order.objects.filter(pk=order.pk).update(customer=owner)

        self.client.post(
            SIGNUP,
            {
                "phone": "9812345678",
                "password": GOOD_PASSWORD,
                "claim_token": order.tracking_token,
            },
            format="json",
        )

        order.refresh_from_db()
        self.assertEqual(order.customer_id, owner.pk)

    def test_an_unknown_token_is_ignored_rather_than_an_error(self):
        """The account still gets created. A stale token in a browser is not a
        reason to refuse someone an account."""
        response = self.client.post(
            SIGNUP,
            {
                "phone": "9812345678",
                "password": GOOD_PASSWORD,
                "claim_token": "not-a-real-tracking-token",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)

    def test_signup_without_a_claim_token_links_nothing(self):
        order = self.place_order(self.product, 1)

        self.client.post(
            SIGNUP, {"phone": "9812345678", "password": GOOD_PASSWORD}, format="json"
        )

        order.refresh_from_db()
        self.assertIsNone(order.customer_id)
