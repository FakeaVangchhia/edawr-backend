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
from django.contrib.auth.hashers import check_password, make_password


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return make_password(password)


def verify_password(password: str, password_hash: str) -> bool:
    # Django returns False for an unrecognised hash format rather than raising,
    # so an old bcrypt row is a failed login, not a 500.
    return check_password(password, password_hash)


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
# Two kinds of caller now hold tokens signed with the *same* secret: the admin
# console and the rider app. A token must therefore say which kind it is, or an
# admin token would authenticate as a rider and vice versa — the `sub` claim
# alone cannot distinguish them (one holds an email, the other a phone number,
# and nothing stops an admin's email from being someone's phone-shaped string).
#
# The claim is `typ`. A token *without* one is treated as an admin token so the
# JWTs minted by the FastAPI backend keep validating, which is the compatibility
# promise this module has always made.
ADMIN_TOKEN = "admin"
RIDER_TOKEN = "rider"


def create_access_token(subject: str, token_type: str = ADMIN_TOKEN) -> str:
    """Sign a JWT whose `sub` claim identifies the caller.

    `sub` is the admin's email for an admin token, and the rider's phone number
    for a rider token. `typ` says which, and every authentication class checks
    it before trusting `sub`.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "typ": token_type,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, expected_type: str) -> str | None:
    """Return the `sub` claim of a valid token of `expected_type`, else None.

    Returning None for a *well-formed token of the wrong type* is what lets two
    authentication classes coexist on one `Authorization: Bearer` header. DRF
    tries each class in order and takes the first non-None result; a class that
    raised on someone else's token would stop the chain before the right class
    ever ran, so "not mine" must be indistinguishable from "no credentials".
    """
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.PyJWTError:
        return None

    if payload.get("typ", ADMIN_TOKEN) != expected_type:
        return None

    return payload.get("sub")
