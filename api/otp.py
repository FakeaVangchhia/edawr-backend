"""The phone-verification challenge, and why it holds no rows.

`Customer.phone_verified_at` has been read everywhere and written nowhere since
the customer accounts landed. The note on that field set the design before this
module existed and this module keeps to it: **the challenge is stateless**, so
switching verification on needs no migration, and the promise made on the model
stays true.

WHAT "STATELESS" COSTS, AND WHY IT IS STILL RIGHT
-------------------------------------------------
A stored challenge gives you an attempt counter for free: three wrong codes and
the row locks. There is no row here, so there is nothing to count and nothing to
lock — **the `otp` throttle is the attempt limit**, and it is the only thing
standing between a six-digit code and a script. Ten an hour per account makes a
million-code space take eleven years; the same design with no throttle would be
broken in a morning. Anyone loosening that scope is removing the lock, not
adjusting a convenience.

What it buys is proportionate to a shop this size: no table to prune, no rows
holding a customer's phone number and a live code side by side, and no migration
between "we cannot verify numbers" and "we can".

WHAT IS IN THE TOKEN
--------------------
The customer's id, the number the code was sent to, and an **HMAC of the code**
under the server's `SECRET_KEY` — never the code. The client holds this string,
so a recoverable code would be no code at all, and a plain SHA-256 would be
barely better: six digits is a million hashes, which is a second of offline work.
The HMAC makes an offline attack need the server secret, which leaves the online
attack, which the throttle governs.

`TimestampSigner` carries the expiry, so an old token is refused by the same call
that authenticates it.

WHAT A VERIFIED NUMBER UNLOCKS
------------------------------
Exactly one thing, and it is worth knowing before changing any of this:
`visible_orders` in `api/views/customer.py` starts showing orders that merely
*carry* the number and belong to no account. Getting verification wrong therefore
hands somebody a stranger's order history — name, address, everything they have
bought — which is why possession of the SIM has to be the thing being proved,
and why setting a password (which proves only that someone typed a number) never
counted.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils.crypto import constant_time_compare, salted_hmac

# Namespaces the signature, so a token minted here can never be unsigned by
# another TimestampSigner in this codebase (or the reverse).
_SALT = "edawr.otp.challenge"

# Separate salt for the code's HMAC: signing and hashing under one key is the
# mistake settings.py already calls out for DJANGO_SECRET_KEY and JWT_SECRET.
_CODE_SALT = "edawr.otp.code"

_SEPARATOR = "|"


class InvalidChallenge(Exception):
    """The token is expired, tampered with, or not this customer's."""


def generate_code() -> str:
    """A numeric code, from the CSPRNG.

    `secrets` rather than `random`: the latter is a Mersenne Twister, and a few
    observed outputs give up its whole future — which for a code that guards an
    order history is the difference between a secret and a formality.

    Zero-padded, because a leading zero is a digit and `%06d` of 1 is `000001`.
    """
    upper = 10 ** settings.OTP_CODE_DIGITS
    return str(secrets.randbelow(upper)).zfill(settings.OTP_CODE_DIGITS)


def _fingerprint(code: str) -> str:
    return salted_hmac(_CODE_SALT, code, algorithm="sha256").hexdigest()


def issue(customer_id: int, phone: str, code: str) -> str:
    """The opaque string the client sends back with the code.

    Binds all three: which account asked, which number was texted, and — as an
    HMAC — which code was sent. Re-checking the phone at verification time is
    what stops a challenge issued for one number from verifying another after
    the customer edits their profile.
    """
    payload = _SEPARATOR.join([str(customer_id), phone, _fingerprint(code)])
    return TimestampSigner(salt=_SALT).sign(payload)


def check(challenge: str, code: str, customer_id: int, phone: str) -> None:
    """Raise `InvalidChallenge` unless this code answers this challenge.

    Returns `None` on success rather than a boolean, so a caller cannot get the
    truth value backwards — the failure is impossible to ignore.
    """
    try:
        payload = TimestampSigner(salt=_SALT).unsign(
            challenge, max_age=settings.OTP_TTL_SECONDS
        )
    except (BadSignature, SignatureExpired) as exc:
        raise InvalidChallenge from exc

    try:
        signed_id, signed_phone, fingerprint = payload.split(_SEPARATOR)
    except ValueError as exc:  # pragma: no cover — only a signed payload reaches here
        raise InvalidChallenge from exc

    if signed_id != str(customer_id) or signed_phone != phone:
        raise InvalidChallenge

    # Constant time, because a comparison that returns early on the first wrong
    # byte leaks how much of a guess was right.
    if not constant_time_compare(fingerprint, _fingerprint(code)):
        raise InvalidChallenge
