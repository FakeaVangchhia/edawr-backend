"""Input normalisation shared by checkout, staff creation and rider login.

The phone number is the only identifier a customer gives us, and it is the key
a rider dials at the door. If "+919812345678", "9812345678" and "098123 45678"
are stored as three different strings, they are three different customers, and
none of them can be found by searching for any of the others. Normalising once,
here, is what keeps that from happening.
"""

from __future__ import annotations

import re

from rest_framework import serializers

# Indian mobile numbers are ten digits starting 6-9. The storefront serves
# Aizawl only, so accepting an arbitrary international format would be accepting
# input we cannot deliver to.
_TEN_DIGIT = re.compile(r"^[6-9]\d{9}$")
_NON_DIGITS = re.compile(r"\D")

E164_PREFIX = "+91"


def normalise_phone(value: str) -> str:
    """Return an Indian mobile number as +91XXXXXXXXXX.

    Accepts the shapes people actually type — spaces, dashes, a leading 0, a
    leading +91 or 91 — and rejects anything that is not a plausible mobile
    number. Raises DRF's ValidationError so it can be used directly as a
    serializer field validator.
    """
    if not value or not value.strip():
        raise serializers.ValidationError("Enter a phone number.")

    digits = _NON_DIGITS.sub("", value)

    # Strip the country code or a trunk-dialling zero, leaving ten digits.
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if not _TEN_DIGIT.match(digits):
        raise serializers.ValidationError(
            "Enter a valid 10-digit Indian mobile number."
        )

    return f"{E164_PREFIX}{digits}"


class PhoneField(serializers.CharField):
    """A CharField that stores a normalised phone number.

    Using a field subclass rather than a `validate_phone` method means every
    serializer that declares a phone gets the same treatment automatically —
    there is no second place to forget it.
    """

    def to_internal_value(self, data) -> str:
        return normalise_phone(super().to_internal_value(data))
