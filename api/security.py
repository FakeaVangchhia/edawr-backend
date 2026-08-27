"""Password hashing and JWT issue/verify.

Same job as the old `app/security.py`, minus the `require_admin` dependency —
that half split into two DRF pieces, `authentication.py` and `permissions.py`.

**Hashing changed.** bcrypt was dropped in favour of
`django.contrib.auth.hashers`, which ships with Django and needs no extra
package. It stores an algorithm-tagged string
(`pbkdf2_sha256$1000000$<salt>$<hash>`) rather than a bare `$2b$` bcrypt digest,
so it can upgrade a password's algorithm transparently on the next successful
login. Those functions are importable without `django.contrib.auth` being in
INSTALLED_APPS — they are plain functions, not an app.

The practical consequence: **hashes written by the FastAPI version cannot be
verified here.** Re-run `uv run manage.py seed`, which recreates the admin.
"""

from datetime import datetime, timedelta, timezone

import jwt
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return make_password(password)


def verify_password(password: str, password_hash: str) -> bool:
    # Django returns False for an unrecognised hash format rather than raising,
    # so an old bcrypt row is a failed login, not a 500.
    return check_password(password, password_hash)


def validate_password_strength(password: str, *, user=None) -> None:
    """Raise DRF's `ValidationError` if `password` is too weak to accept.

    One implementation, called by every place that *sets* a password — sign-up
    and password change — so the two cannot drift into disagreeing about what
    is allowed. Nothing that *checks* a password calls this: a rule added later
    must not lock out an account whose existing password predates it, and the
    length of a rejected login attempt is a free hint about the policy.

    The rules come from Django's own validators (configured in
    `AUTH_PASSWORD_VALIDATORS`), which work without `django.contrib.auth` being
    an installed app for the same reason the hashers do — they are plain
    functions. That buys four rules and their wording for free, and one of them
    matters here far more than the rest: **`NumericPasswordValidator`**. This
    store identifies a customer *by their phone number*, and a customer asked
    for a password will reach for ten digits. A ten-digit number on an account
    keyed by a ten-digit number is close to no password at all.

    Pass `user=` an unsaved model instance carrying the phone and name, and
    `UserAttributeSimilarityValidator` will refuse a password that is simply
    those. It reads attributes off whatever it is given; it does not need a row.
    """
    try:
        password_validation.validate_password(password, user=user)
    except DjangoValidationError as exc:
        # Django raises its own ValidationError with `.messages`; DRF's
        # exception handler only understands DRF's. Re-raising is what turns
        # this into a 400 with the project's `{"detail": ...}` shape rather
        # than an unhandled 500.
        raise serializers.ValidationError(list(exc.messages)) from exc


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
# Three kinds of caller now hold tokens signed with the *same* secret: the admin
# console, the rider app and a signed-in customer. A token must therefore say
# which kind it is, or an admin token would authenticate as a rider and vice
# versa — the `sub` claim alone cannot distinguish them (one holds an email, the
# others a phone number, and nothing stops an admin's email from being someone's
# phone-shaped string).
#
# **With customers that stopped being hypothetical.** A rider token and a
# customer token both carry a `+91` phone number in `sub`, and in a town this
# size the *same* number can legitimately be both: a rider who shops at the shop
# they deliver for has a row in `users` and a row in `customers`. The two are
# separate accounts with separate passwords, and `typ` is the only thing
# standing between them. `api/tests/test_auth.py` pins that with a rider and a
# customer who share a number.
#
# The claim is `typ`. A token *without* one is treated as an admin token so the
# JWTs minted by the FastAPI backend keep validating, which is the compatibility
# promise this module has always made.
ADMIN_TOKEN = "admin"
RIDER_TOKEN = "rider"
CUSTOMER_TOKEN = "customer"


def create_access_token(
    subject: str,
    token_type: str = ADMIN_TOKEN,
    *,
    version: int = 0,
    session_started_at: datetime | None = None,
) -> str:
    """Sign a JWT whose `sub` claim identifies the caller.

    `sub` is the admin's email for an admin token, and the rider's phone number
    for a rider token. `typ` says which, and every authentication class checks
    it before trusting `sub`.

    Two more claims exist to bound how long a leaked token stays useful, because
    a bearer token the server does not store cannot otherwise be taken back:

    `ver` is the account's `token_version` column. The authentication class
    compares the two on every request, so incrementing the column retires every
    token that account holds. That is what `POST /api/auth/logout` does.

    `ait` — *auth issued at* — is when the **session** began, as distinct from
    `iat`, which is when this particular token was signed. `/api/auth/me` mints
    a fresh token on every call, so without a claim that survives the refresh a
    session renews itself forever and a stolen token never expires. `ait` is
    copied forward unchanged across refreshes; `MeView` refuses to renew past
    `SESSION_MAX_HOURS` from it, which forces a real sign-in eventually.
    """
    now = datetime.now(timezone.utc)
    started = session_started_at or now
    payload = {
        "sub": subject,
        "typ": token_type,
        "ver": version,
        "ait": int(started.timestamp()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, expected_type: str) -> dict | None:
    """Return the claims of a valid token of `expected_type`, else None.

    Returning None for a *well-formed token of the wrong type* is what lets two
    authentication classes coexist on one `Authorization: Bearer` header. DRF
    tries each class in order and takes the first non-None result; a class that
    raised on someone else's token would stop the chain before the right class
    ever ran, so "not mine" must be indistinguishable from "no credentials".

    The whole payload comes back rather than just `sub`, because the caller now
    has to check `ver` against the account row and read `ait` — and a decoder
    that returns one claim while the caller needs three invites a second,
    unverified `jwt.decode` somewhere else in the codebase.
    """
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    # `TypeError` and `ValueError` alongside PyJWT's own hierarchy, because
    # PyJWT's claim validation does not wrap everything: a token carrying
    # `"iat": null` reaches `int(payload["iat"])` and raises a bare `TypeError`,
    # which would escape this function and surface as a 500 on every request
    # carrying it. Signature verification runs first, so producing such a token
    # needs the signing key — but "unparseable" and "not mine" should be the
    # same answer whoever asks, and an authentication path that can raise an
    # unhandled exception is one bad claim away from an outage.
    except (jwt.PyJWTError, TypeError, ValueError):
        return None

    if payload.get("typ", ADMIN_TOKEN) != expected_type:
        return None

    if not isinstance(payload.get("sub"), str):
        return None

    return payload


def session_started(payload: dict) -> datetime:
    """When the session behind this token began.

    Falls back to `iat` for a token minted before `ait` existed, so the deploy
    that introduced it does not sign everyone out mid-shift — their session
    clock simply starts from whenever their current token was issued.
    """
    stamp = payload.get("ait") or payload.get("iat") or 0
    return datetime.fromtimestamp(int(stamp), tz=timezone.utc)
