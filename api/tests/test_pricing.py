"""Pricing arithmetic — the part where a bug costs real money."""

from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from api.pricing import (
    compute_charges,
    default_tier,
    delivery_tiers,
    free_delivery_shortfall,
    money,
    resolve_tier,
)


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


TIER_SETTINGS = dict(
    DELIVERY_FEE_INSTANT="15.00",
    DELIVERY_FEE_SLOW="5.00",
    DELIVERY_PROMISE_MINUTES_INSTANT=15,
    DELIVERY_PROMISE_MINUTES_SLOW=45,
    DEFAULT_DELIVERY_TYPE="instant",
    FREE_DELIVERY_ABOVE="199.00",
    HANDLING_FEE="5.00",
)


@override_settings(**TIER_SETTINGS)
class ChargeTests(SimpleTestCase):
    def test_small_basket_pays_delivery_and_handling(self):
        charges = compute_charges(Decimal("100.00"), "instant")
        self.assertEqual(charges.delivery_fee, Decimal("15.00"))
        self.assertEqual(charges.handling_fee, Decimal("5.00"))
        self.assertEqual(charges.grand_total, Decimal("120.00"))

    def test_slow_is_ten_rupees_cheaper(self):
        charges = compute_charges(Decimal("100.00"), "slow")
        self.assertEqual(charges.delivery_fee, Decimal("5.00"))
        self.assertEqual(charges.grand_total, Decimal("110.00"))

    def test_delivery_is_free_at_the_threshold_exactly(self):
        """Boundary: 'above 199' is implemented as >= 199, and the storefront
        promises free delivery *at* that number."""
        charges = compute_charges(Decimal("199.00"), "instant")
        self.assertEqual(charges.delivery_fee, Decimal("0.00"))
        self.assertEqual(charges.grand_total, Decimal("204.00"))

    def test_the_threshold_frees_both_tiers(self):
        """A basket past the threshold costs the same either way — which is the
        whole reason the picker can stop nagging once you are over it."""
        for key in ("instant", "slow"):
            with self.subTest(delivery_type=key):
                charges = compute_charges(Decimal("250.00"), key)
                self.assertEqual(charges.delivery_fee, Decimal("0.00"))
                self.assertEqual(charges.grand_total, Decimal("255.00"))

    def test_one_paisa_below_the_threshold_still_pays(self):
        self.assertEqual(
            compute_charges(Decimal("198.99"), "instant").delivery_fee,
            Decimal("15.00"),
        )
        self.assertEqual(
            compute_charges(Decimal("198.99"), "slow").delivery_fee,
            Decimal("5.00"),
        )

    def test_empty_basket_is_free(self):
        charges = compute_charges(Decimal("0.00"), "instant")
        self.assertEqual(charges.grand_total, Decimal("0.00"))
        self.assertEqual(charges.handling_fee, Decimal("0.00"))

    def test_totals_do_not_drift_over_many_lines(self):
        """The float version of this sum is 143.00000000000003."""
        items = sum([Decimal("62.10"), Decimal("35.30"), Decimal("45.60")], Decimal("0"))
        charges = compute_charges(items, "instant")
        self.assertEqual(charges.items_total, Decimal("143.00"))
        self.assertEqual(charges.grand_total, Decimal("163.00"))

    def test_shortfall_counts_down_to_free_delivery(self):
        self.assertEqual(free_delivery_shortfall(Decimal("150.00")), Decimal("49.00"))
        self.assertEqual(free_delivery_shortfall(Decimal("199.00")), Decimal("0.00"))
        self.assertEqual(free_delivery_shortfall(Decimal("250.00")), Decimal("0.00"))


@override_settings(**TIER_SETTINGS)
class DeliveryTierTests(SimpleTestCase):
    def test_tiers_are_listed_fastest_first(self):
        keys = [tier.key for tier in delivery_tiers()]
        self.assertEqual(keys, ["instant", "slow"])

    def test_each_tier_carries_its_own_window(self):
        by_key = {tier.key: tier for tier in delivery_tiers()}
        self.assertEqual(by_key["instant"].promise_minutes, 15)
        self.assertEqual(by_key["slow"].promise_minutes, 45)
        self.assertEqual(by_key["instant"].fee, Decimal("15.00"))
        self.assertEqual(by_key["slow"].fee, Decimal("5.00"))

    def test_no_tier_named_resolves_to_the_default(self):
        self.assertEqual(resolve_tier(None).key, "instant")
        self.assertEqual(resolve_tier("").key, "instant")

    def test_unknown_tier_resolves_upward_not_downward(self):
        """The fallback must never be the cheap, slow tier: quietly giving
        someone a 45-minute delivery they think is 15 is the expensive
        mistake."""
        self.assertEqual(resolve_tier("banana").key, "instant")
        self.assertEqual(compute_charges(Decimal("100.00"), "banana").delivery_fee,
                         Decimal("15.00"))

    @override_settings(DEFAULT_DELIVERY_TYPE="slow")
    def test_the_default_tier_is_configurable(self):
        self.assertEqual(default_tier().key, "slow")
        self.assertEqual(compute_charges(Decimal("100.00")).delivery_fee, Decimal("5.00"))

    @override_settings(DEFAULT_DELIVERY_TYPE="nonsense")
    def test_a_misconfigured_default_falls_back_to_the_fastest_tier(self):
        """A typo in the environment must not take the store down, and erring
        fast is the safe direction."""
        self.assertEqual(default_tier().key, "instant")
