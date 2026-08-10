"""Who is making this request?

DRF splits the old `require_admin` dependency in two, and the split is the main
idea to carry over from FastAPI:

    authentication  ->  "who is this?"   sets request.user. Never rejects.
    permission      ->  "may they?"      rejects. See permissions.py.

An authentication class runs on *every* request (it is listed in
`REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`). It returns `(user, auth)`
if it recognises the credentials, or `None` to mean "no credentials of my kind
here — carry on", which leaves `request.user` as None.

It raises `AuthenticationFailed` only for credentials that are *present but
bad*: a malformed, forged or expired token is an error worth reporting, whereas
a missing token is simply an anonymous request that a public endpoint should
still serve.
"""

from rest_framework import authentication, exceptions

from api.models import AdminUser, User
from api.security import ADMIN_TOKEN, RIDER_TOKEN, decode_token


class BearerJWTAuthentication(authentication.BaseAuthentication):
    """Shared plumbing for the two bearer schemes.

    Both subclasses read the same header and differ only in which token `typ`
    they accept and how they turn a `sub` claim into a user row.

    **Both classes run on every request**, in the order they are listed in
    `DEFAULT_AUTHENTICATION_CLASSES`. DRF stops at the first one that returns a
    user, so each must return None — never raise — for a valid token belonging
    to the *other* scheme. `decode_token` enforces that by returning None on a
    type mismatch.
    """

    keyword = "Bearer"
    token_type: str

    def resolve(self, subject: str):
        """Map a `sub` claim to a user object, or None if there is no match."""
        raise NotImplementedError

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1")
        if not header:
            return None  # anonymous; public endpoints still work

        parts = header.split()
        if parts[0].lower() != self.keyword.lower():
            return None  # some other scheme — not ours to judge
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed(
                "Authorization header must be 'Bearer <token>'."
            )

        subject = decode_token(parts[1], self.token_type)
        if subject is None:
            # Either a bad token or one meant for the other scheme. Staying
            # quiet lets the next authentication class try; if none of them
            # recognise it, the permission class produces the 401.
            return None

        user = self.resolve(subject)
        if user is None:
            raise exceptions.AuthenticationFailed("Invalid or expired token.")

        # The second element becomes `request.auth`. Returning the raw token
        # keeps it available to a view that wants to re-sign or inspect it.
        return (user, parts[1])

    def authenticate_header(self, request):
        """The `WWW-Authenticate` value.

        Returning a non-empty string here is what makes DRF answer a rejected
        request with 401 rather than 403. Without it, a missing token would show
        up in the frontend as "Forbidden" and the stored-session check in
        `AdminLogin.tsx` would not clear the session.
        """
        return self.keyword


class AdminJWTAuthentication(BearerJWTAuthentication):
    """Resolves an admin token's `sub` (an email) to an AdminUser."""

    token_type = ADMIN_TOKEN

    def resolve(self, subject: str):
        return AdminUser.objects.filter(email=subject, is_active=True).first()


class RiderJWTAuthentication(BearerJWTAuthentication):
    """Resolves a rider token's `sub` (a phone number) to a delivery User.

    Neither filter is redundant with the login view's own checks, because both
    conditions can change *after* a token was issued and tokens live for twelve
    hours. A rider demoted to manager must stop being a rider immediately, and a
    rider deactivated when they leave must stop delivering immediately — not
    whenever their token happens to expire. Re-checking on every request is what
    makes `is_active = False` an actual revocation.
    """

    token_type = RIDER_TOKEN

    def resolve(self, subject: str):
        return User.objects.filter(
            phone=subject, role=User.DELIVERY, is_active=True
        ).first()
