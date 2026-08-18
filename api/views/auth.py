"""Admin and rider login.

The frontend POSTs {email, password} to /api/auth/login and expects
{access_token, username}. `AdminLogin.tsx` caches that in sessionStorage and
`authFetch` replays the token as `Authorization: Bearer <token>`.

The mobile app POSTs {phone, pin} to /api/auth/rider/login and expects
{access_token, rider}. Both tokens are signed with the same secret and told
apart by their `typ` claim — see api/security.py.
"""

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import AdminUser, User
from api.permissions import IsAdmin, IsRider
from api.security import RIDER_TOKEN, create_access_token, verify_password
from api.serializers import (
    LoginResponseSerializer,
    LoginSerializer,
    RiderLoginResponseSerializer,
    RiderLoginSerializer,
    UserSerializer,
)


def _token_response(admin: AdminUser) -> Response:
    """The console session payload.

    `role` is here so the console knows which navigation to draw. It is *not* a
    permission: the token carries no role claim, and `IsOwnerAdmin` re-reads the
    row on every request. A client that tampered with its stored copy would gain
    a menu item that 403s. See the comment on `AdminUser.role`.

    `username` is retained, unchanged, because the storefront's existing admin
    screen reads it and knows nothing about any of the rest.
    """
    return Response(
        {
            "access_token": create_access_token(admin.email),
            "token_type": "bearer",
            "username": admin.email,
            "email": admin.email,
            "name": admin.name,
            "role": admin.role,
        }
    )


def _rider_token_response(rider: User) -> Response:
    return Response(
        {
            "access_token": create_access_token(rider.phone, token_type=RIDER_TOKEN),
            "token_type": "bearer",
            # The full profile, because the app needs the rider's id and service
            # radius straight after login. UserSerializer omits `pin_hash`.
            "rider": UserSerializer(rider).data,
        }
    )


class LoginView(APIView):
    """POST /api/auth/login

    Note this view has no `permission_classes` — it inherits the project default
    of `AllowAny`, because it is how you get a token in the first place. Keeping
    it in its own class next to a guarded sibling is the DRF equivalent of
    keeping `/login` in a router with no router-level dependency.

    `throttle_scope` activates the ScopedRateThrottle configured in settings —
    without this attribute the throttle class ignores the view entirely.
    """

    throttle_scope = "login"

    @extend_schema(request=LoginSerializer, responses=LoginResponseSerializer)
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        # raise_exception=True is the line that makes DRF behave like FastAPI:
        # a bad body becomes a 400 response and nothing below this runs.
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()
        password = serializer.validated_data["password"]

        admin = AdminUser.objects.filter(email=email, is_active=True).first()

        # Same error whether the email is unknown or the password is wrong, so
        # the response cannot be used to enumerate which admin emails exist.
        if admin is None or not verify_password(password, admin.password_hash):
            return Response(
                {"detail": "Incorrect email or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Stamped on the way past, with update_fields so a login cannot clobber a
        # concurrent edit to this account's role or name. Not audited: a row per
        # sign-in would bury the changes the log exists to surface, and the
        # console shows this column on the accounts screen instead.
        admin.last_login_at = timezone.now()
        admin.save(update_fields=["last_login_at"])

        return _token_response(admin)


class MeView(APIView):
    """GET /api/auth/me

    Lets the frontend check whether a stored token is still valid, and issues a
    fresh one. Guarded per-view, since the sibling /login must stay public.

    `request.user` is the AdminUser that `AdminJWTAuthentication` resolved — the
    return value of the old `require_admin` dependency, delivered by attribute
    rather than by parameter.
    """

    permission_classes = [IsAdmin]

    @extend_schema(responses=LoginResponseSerializer)
    def get(self, request):
        return _token_response(request.user)


class RiderLoginView(APIView):
    """POST /api/auth/rider/login — phone + PIN, public like its admin sibling.

    Shares the `login` throttle scope with the admin route: a PIN is short
    enough that rate limiting is what makes it a credential rather than a
    formality.
    """

    throttle_scope = "login"

    @extend_schema(request=RiderLoginSerializer, responses=RiderLoginResponseSerializer)
    def post(self, request):
        serializer = RiderLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data["phone"].strip()
        pin = serializer.validated_data["pin"]

        # `is_active` is checked here as well as in RiderJWTAuthentication.
        # Without it a dismissed rider could still sign in and mint a fresh
        # token — the per-request check would then reject every call, but the
        # login screen would say the credentials were fine, which is a confusing
        # way to be locked out and leaves a valid credential in play.
        rider = User.objects.filter(
            phone=phone, role=User.DELIVERY, is_active=True
        ).first()

        # One message for every failure — unknown phone, wrong PIN, a deactivated
        # rider, or one with no PIN set — so the response body cannot be used to
        # discover which phone numbers belong to riders. (Response *timing* still can:
        # an unknown phone skips the PBKDF2 verify and returns sooner. Closing
        # that would mean hashing against a dummy digest on every miss; it is
        # not done here because a four-digit PIN is the weaker link by far.)
        if rider is None or not rider.pin_hash or not verify_password(pin, rider.pin_hash):
            return Response(
                {"detail": "Incorrect phone or PIN."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return _rider_token_response(rider)


class RiderMeView(APIView):
    """GET /api/auth/rider/me — validate a stored rider token, refresh it."""

    permission_classes = [IsRider]

    @extend_schema(responses=RiderLoginResponseSerializer)
    def get(self, request):
        return _rider_token_response(request.user)
