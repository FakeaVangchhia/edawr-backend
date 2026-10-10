"""Proving a customer holds the SIM, not just the number.

The stakes are set by one rule in `api/views/customer.py::visible_orders`: a
verified account additionally sees orders that merely *carry* its number and
belong to nobody. Get verification wrong and that is a stranger's order history
— their name, their address, everything they have bought — handed over on the
strength of a typed number. Which is why setting a password never counted, and
why every negative case below matters more than the happy path.

The challenge is stateless by design (see `api/otp.py`), which moves two things
into these tests that a stored challenge would have proved on its own: that an
expired token is refused, and that nothing anywhere stores the code.
"""

from unittest.mock import patch

from django.test import override_settings

from api import otp
from api.tests.base import CUSTOMER_PHONE, APITestBase

CHALLENGE = "/api/customer/phone/challenge"
VERIFY = "/api/customer/phone/verify"


def sent_code(logs) -> str:
    """The code, out of the console backend's log line.

    The application deliberately never returns one, so a test has to read it the
    way a developer does. That this is awkward is the point: if it were easy to
    get at from the outside, so would the code be.
    """
    message = logs.output[0]
    digits = [part for part in message.replace(",", " ").split() if part.isdigit()]
    return digits[0]


@override_settings(SMS_BACKEND="console")
class PhoneChallengeTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.customer = self.as_customer()

    def test_a_challenge_is_issued_and_carries_no_code(self):
        with self.assertLogs("api.sms", level="WARNING") as logs:
            response = self.client.post(CHALLENGE, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["phone"], self.customer.phone)
        self.assertIn("challenge", response.data)

        code = sent_code(logs)
        # The whole security of a client-held token: it proves which code was
        # sent without containing it.
        self.assertNotIn(code, response.data["challenge"])
        self.assertNotIn("code", response.data)

    def test_the_body_cannot_name_a_number(self):
        """The number comes from the token's row, like everything else here."""
        with self.assertLogs("api.sms", level="WARNING") as logs:
            response = self.client.post(
                CHALLENGE, {"phone": "+919999999999"}, format="json"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["phone"], self.customer.phone)
        self.assertEqual(logs.records[0].phone, self.customer.phone)

    def test_a_guest_cannot_ask_for_one(self):
        self.as_anonymous()
        self.assertEqual(self.client.post(CHALLENGE, {}, format="json").status_code, 401)

    def test_an_already_verified_number_is_a_conflict(self):
        self.customer.phone_verified_at = self.customer.created_at
        self.customer.save(update_fields=["phone_verified_at"])

        response = self.client.post(CHALLENGE, {}, format="json")
        self.assertEqual(response.status_code, 409)

    @override_settings(SMS_BACKEND="disabled")
    def test_no_provider_is_a_503_and_not_a_silent_200(self):
        """The state of every deployment today.

        Answering 200 would leave a customer watching a phone that was never
        going to ring, tapping resend into the same silence.
        """
        response = self.client.post(CHALLENGE, {}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertIn("deployment.md", response.data["detail"])


@override_settings(SMS_BACKEND="console")
class PhoneVerifyTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.customer = self.as_customer()

    def challenge(self):
        with self.assertLogs("api.sms", level="WARNING") as logs:
            response = self.client.post(CHALLENGE, {}, format="json")
        return response.data["challenge"], sent_code(logs)

    def test_the_right_code_verifies_the_number(self):
        challenge, code = self.challenge()

        response = self.client.post(
            VERIFY, {"challenge": challenge, "code": code}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["phone_verified"])
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.phone_verified_at)

    def test_a_wrong_code_is_refused_and_changes_nothing(self):
        challenge, code = self.challenge()
        wrong = "000000" if code != "000000" else "111111"

        response = self.client.post(
            VERIFY, {"challenge": challenge, "code": wrong}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.phone_verified_at)

    def test_a_tampered_challenge_is_refused(self):
        challenge, code = self.challenge()

        response = self.client.post(
            VERIFY, {"challenge": challenge + "x", "code": code}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_an_expired_challenge_is_refused(self):
        """The expiry is in the signature, so there is no row to prune."""
        challenge, code = self.challenge()

        with override_settings(OTP_TTL_SECONDS=-1):
            response = self.client.post(
                VERIFY, {"challenge": challenge, "code": code}, format="json"
            )

        self.assertEqual(response.status_code, 400)

    def test_another_account_cannot_use_a_challenge_issued_to_this_one(self):
        """The customer id is inside the signature, not only in the token.

        Without that, a challenge is a bearer credential for whoever holds it —
        and the person holding it is by definition not the person whose phone
        rang.
        """
        challenge, code = self.challenge()

        other = self.make_customer(phone="+919000000999", name="Someone Else")
        self.as_customer(other)

        response = self.client.post(
            VERIFY, {"challenge": challenge, "code": code}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        other.refresh_from_db()
        self.assertIsNone(other.phone_verified_at)

    def test_changing_the_number_invalidates_an_outstanding_challenge(self):
        """The number is signed in too.

        Otherwise a code texted to a number the customer controls could verify a
        different number they typed in afterwards — which is the attack this
        endpoint exists to prevent, arriving through the back door.
        """
        challenge, code = self.challenge()

        self.customer.phone = "+919000000102"
        self.customer.save(update_fields=["phone"])
        self.as_customer(self.customer)

        response = self.client.post(
            VERIFY, {"challenge": challenge, "code": code}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_verifying_twice_is_harmless(self):
        """A stateless challenge can be replayed until it expires.

        That is a real property of the design, and the reason it is acceptable:
        the outcome of a replay is the outcome that already happened. Nothing is
        incremented and nothing is re-stamped.
        """
        challenge, code = self.challenge()
        first = self.client.post(
            VERIFY, {"challenge": challenge, "code": code}, format="json"
        )
        self.customer.refresh_from_db()
        stamped = self.customer.phone_verified_at

        second = self.client.post(
            VERIFY, {"challenge": challenge, "code": code}, format="json"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone_verified_at, stamped)

    def test_a_guest_cannot_verify(self):
        self.as_anonymous()
        response = self.client.post(
            VERIFY, {"challenge": "x", "code": "123456"}, format="json"
        )
        self.assertEqual(response.status_code, 401)


@override_settings(SMS_BACKEND="console")
class VerificationUnlocksHistoryTests(APITestBase):
    """What the whole feature is for, and why it is guarded this hard."""

    def test_an_unclaimed_order_appears_only_once_the_number_is_verified(self):
        product = self.make_product(price="62.00", stock=50)
        # An order placed as a guest, carrying this customer's number and
        # belonging to no account — the case `visible_orders` rule 2 covers.
        order = self.place_order(product, 1, customer_phone=CUSTOMER_PHONE)
        customer = self.make_customer(phone=CUSTOMER_PHONE, verified=False)
        self.as_customer(customer)

        before = self.client.get("/api/customer/orders")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.data, [])

        with self.assertLogs("api.sms", level="WARNING") as logs:
            challenge = self.client.post(CHALLENGE, {}, format="json").data["challenge"]
        code = sent_code(logs)
        self.client.post(VERIFY, {"challenge": challenge, "code": code}, format="json")

        after = self.client.get("/api/customer/orders")
        self.assertEqual(len(after.data), 1)
        # Still nobody's, in the database. Verification changes what this
        # account may *see*, not who the order belongs to — claiming is the
        # separate act, and it needs the tracking token.
        order.refresh_from_db()
        self.assertIsNone(order.customer_id)


class OtpTokenTests(APITestBase):
    """The signing itself, without the HTTP layer in the way."""

    def test_a_code_is_the_configured_length_and_keeps_its_leading_zeros(self):
        with patch("api.otp.secrets.randbelow", return_value=1):
            self.assertEqual(otp.generate_code(), "000001")

    def test_the_code_is_not_recoverable_from_the_challenge(self):
        """The client holds this string, so a recoverable code is no code.

        It is an HMAC under the server's SECRET_KEY rather than a bare hash: six
        digits is a million hashes, which is a second of offline work for anyone
        who got hold of a token.
        """
        challenge = otp.issue(1, "+919000000101", "123456")
        self.assertNotIn("123456", challenge)

    def test_check_raises_rather_than_returning_false(self):
        challenge = otp.issue(1, "+919000000101", "123456")
        # The success path returns None, so a caller cannot get the truth value
        # backwards and let a wrong code through.
        self.assertIsNone(otp.check(challenge, "123456", 1, "+919000000101"))
        with self.assertRaises(otp.InvalidChallenge):
            otp.check(challenge, "654321", 1, "+919000000101")
