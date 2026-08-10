"""Pricing arithmetic — the part where a bug costs real money."""

from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from api.pricing import compute_charges, free_delivery_shortfall, money


class MoneyTests(SimpleTestCase):
    def test_quantises_to_two_places(self):
        self.assertEqual(money(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(money(Decimal("1.004")), Decimal("1.00"))

    def test_rounds_half_up_not_half_even(self):
        """Python's default rounding would give 0.12 here, and a customer
        checking the bill by hand would get 0.13."""
        self.assertEqual(money(Decimal("0.125")), Decimal("0.13"))
        self.assertEqual(money(Decimal("0.135")), Decimal("0.14"))

    def test_float_input_does_not_inherit_binary_error(self):
        """Decimal(0.1) is 0.1000000000000000055511151231257827."""
        self.assertEqual(money(0.1), Decimal("0.10"))
        self.assertEqual(money(2.675), Decimal("2.68"))

    def test_string_and_int_input(self):
        self.assertEqual(money("62"), Decimal("62.00"))
        self.assertEqual(money(62), Decimal("62.00"))


@override_settings(
    DELIVERY_FEE="25.00",
    FREE_DELIVERY_ABOVE="199.00",
    HANDLING_FEE="5.00",
)
class ChargeTests(SimpleTestCase):
    def test_small_basket_pays_delivery_and_handling(self):
        charges = compute_charges(Decimal("100.00"))
        self.assertEqual(charges.delivery_fee, Decimal("25.00"))
        self.assertEqual(charges.handling_fee, Decimal("5.00"))
        self.assertEqual(charges.grand_total, Decimal("130.00"))

    def test_delivery_is_free_at_the_threshold_exactly(self):
        """Boundary: 'above 199' is implemented as >= 199, and the storefront
        promises free delivery *at* that number."""
        charges = compute_charges(Decimal("199.00"))
        self.assertEqual(charges.delivery_fee, Decimal("0.00"))
        self.assertEqual(charges.grand_total, Decimal("204.00"))

    def test_one_paisa_below_the_threshold_still_pays(self):
        charges = compute_charges(Decimal("198.99"))
        self.assertEqual(charges.delivery_fee, Decimal("25.00"))

    def test_empty_basket_is_free(self):
        charges = compute_charges(Decimal("0.00"))
        self.assertEqual(charges.grand_total, Decimal("0.00"))
        self.assertEqual(charges.handling_fee, Decimal("0.00"))

    def test_totals_do_not_drift_over_many_lines(self):
        """The float version of this sum is 143.00000000000003."""
        items = sum([Decimal("62.10"), Decimal("35.30"), Decimal("45.60")], Decimal("0"))
        charges = compute_charges(items)
        self.assertEqual(charges.items_total, Decimal("143.00"))
        self.assertEqual(charges.grand_total, Decimal("173.00"))

    def test_shortfall_counts_down_to_free_delivery(self):
        self.assertEqual(free_delivery_shortfall(Decimal("150.00")), Decimal("49.00"))
        self.assertEqual(free_delivery_shortfall(Decimal("199.00")), Decimal("0.00"))
        self.assertEqual(free_delivery_shortfall(Decimal("250.00")), Decimal("0.00"))
