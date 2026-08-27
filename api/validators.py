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


# --------------------------------------------------------------------------
# Coordinates
# --------------------------------------------------------------------------
# Aizawl, for reference: 23.72 N, 92.71 E.


def require_both_or_neither(latitude, longitude) -> None:
    """A position is optional, but half of one is not.

    Latitude without longitude is not a partial answer, it is a bug in the
    client — and storing it as "unknown" would hide that bug rather than report
    it. Shared by checkout, where a position is optional, and by the live
    location endpoints, where it is not; both need the same sentence.
    """
    if (latitude is None) != (longitude is None):
        raise serializers.ValidationError(
            "Send both customer_latitude and customer_longitude, or neither."
        )


def reject_null_island(latitude, longitude) -> None:
    """Refuse the exact pair (0, 0).

    Null Island is in the Gulf of Guinea, about 6,000 km from Aizawl, and no
    handset is ever legitimately there. It is what a *failed* fix serialises to
    when a device reports a zeroed struct instead of an error — a real failure
    mode of cheap Android GPS chips, and of a browser geolocation call that
    resolves with an empty position.

    Only the live-location endpoints use this. Checkout does not need it: a
    position that far out is already refused by the delivery-radius check, and
    changing which message that produces would change tested behaviour for no
    gain. Here there is no radius check to fall through to — an accepted (0, 0)
    would put a rider marker in the Atlantic and compute a 6,000 km distance to
    the customer, both of which look like data rather than like the error they
    are.
    """
    if latitude == 0 and longitude == 0:
        raise serializers.ValidationError(
            "Received an empty position fix (0, 0). Wait for a real GPS lock."
        )
