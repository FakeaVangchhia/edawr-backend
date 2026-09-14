"""The poll sticker's answers: written by anyone, read by the console.

The one rule worth a test of its own is the same one checkout has — the
account on the row comes from the token, never from the body. A public
endpoint that accepted a `customer` field would let anyone attach a suggestion
to anyone's account.
"""

from unittest.mock import patch

from rest_framework.throttling import SimpleRateThrottle

from api.models import Suggestion
from api.tests.base import APITestBase

PUBLIC_URL = "/api/store/suggestions"
ADMIN_URL = "/api/suggestions"


def with_throttle_rates(**rates):
    """Duplicated from test_throttling.py for the same reason it is duplicated there."""
    return patch.dict(SimpleRateThrottle.THROTTLE_RATES, rates)


class SuggestionCreateTests(APITestBase):
    def test_guest_can_answer(self):
        response = self.client.post(PUBLIC_URL, {"text": "  Hot   momos "}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        suggestion = Suggestion.objects.get(pk=response.data["id"])
        self.assertEqual(suggestion.text, "Hot momos")
        self.assertIsNone(suggestion.customer)

    def test_blank_and_long_are_400(self):
        for body in [{}, {"text": ""}, {"text": "   \n  "}, {"text": "x" * 281}]:
            response = self.client.post(PUBLIC_URL, body, format="json")
            self.assertEqual(response.status_code, 400, body)
            self.assertIn("detail", response.data)
        self.assertEqual(Suggestion.objects.count(), 0)

    def test_customer_comes_from_the_token_not_the_body(self):
        customer = self.as_customer()
        other = self.make_customer(phone="+919000000102")
        response = self.client.post(
            PUBLIC_URL, {"text": "Ice cream", "customer": other.pk}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Suggestion.objects.get().customer_id, customer.pk)

    def test_an_admin_answering_is_a_guest(self):
        self.as_admin()
        self.client.post(PUBLIC_URL, {"text": "Test"}, format="json")
        self.assertIsNone(Suggestion.objects.get().customer)

    @with_throttle_rates(suggestions="2/min")
    def test_throttled_per_caller(self):
        self.assertEqual(self.client.post(PUBLIC_URL, {"text": "a"}, format="json").status_code, 201)
        self.assertEqual(self.client.post(PUBLIC_URL, {"text": "b"}, format="json").status_code, 201)
        self.assertEqual(self.client.post(PUBLIC_URL, {"text": "c"}, format="json").status_code, 429)


class SuggestionListTests(APITestBase):
    def test_console_reads_newest_first_with_the_account(self):
        customer = self.make_customer(name="Lal")
        Suggestion.objects.create(text="Older")
        Suggestion.objects.create(text="Newer", customer=customer)

        self.as_manager()
        response = self.client.get(ADMIN_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Total-Count"], "2")
        newer, older = response.data
        self.assertEqual(newer["text"], "Newer")
        self.assertEqual(newer["customer_phone"], customer.phone)
        self.assertEqual(newer["customer_name"], "Lal")
        self.assertIsNone(older["customer_phone"])

    def test_list_is_guarded(self):
        self.assertEqual(self.client.get(ADMIN_URL).status_code, 401)
        self.as_customer()
        self.assertEqual(self.client.get(ADMIN_URL).status_code, 403)
