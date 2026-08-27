"""Admin and rider login, refresh and sign-out.

The frontend POSTs {email, password} to /api/auth/login and expects
{access_token, username}. `AdminLogin.tsx` caches that in sessionStorage and
`authFetch` replays the token as `Authorization: Bearer <token>`.

The mobile app POSTs {phone, pin} to /api/auth/rider/login and expects
{access_token, rider}. Both tokens are signed with the same secret and told
apart by their `typ` claim — see api/security.py.

**Signing out is a server-side operation here, not just a client deleting its
copy.** A JWT is a bearer credential the API does not store, so nothing about
possessing one can be taken back by clearing localStorage — the token keeps
working for the rest of its twelve hours in anyone else's hands. `/logout`
increments the account's `token_version`, which every request compares against
the `ver` claim, so the credential stops working the moment the button is
pressed. It retires every device that account is signed in on, which is the
right trade at one console per person and one phone per rider; per-token
revocation would need a blacklist that outlives the token.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import AdminUser, Customer, Order, User
from api.permissions import CustomerAPIView, IsAdmin, IsCustomer, IsRider
from api.security import (
    ADMIN_TOKEN,
    CUSTOMER_TOKEN,
    RIDER_TOKEN,
    create_access_token,
    decode_token,
    hash_password,
    session_started,
    verify_password,
)
from api.serializers import (
    CustomerLoginSerializer,
    CustomerPasswordSerializer,
    CustomerProfileSerializer,
    CustomerSerializer,
    CustomerSignupSerializer,
    CustomerTokenResponseSerializer,
    LoginResponseSerializer,
    LoginSerializer,
    RiderLoginResponseSerializer,
    RiderLoginSerializer,
    UserSerializer,
)


logger = logging.getLogger(__name__)


def _session_expired() -> Response:
    """401, so the client clears its stored session and shows the login screen.

    Not 403. The distinction this project draws everywhere: 401 means "I do not
    know who you are", which is what makes a client discard its credentials;
    403 means "I know who you are and you may not do this", which must leave an
    admin signed in on a page they merely lack rights for.
    """
    return Response(
        {"detail": "Your session has expired. Please sign in again."},
        status=status.HTTP_401_UNAUTHORIZED,
    )


def _token_response(admin: AdminUser, *, session_started_at=None) -> Response:
    """The console session payload.

    `role` is here so the console knows which navigation to draw. It is *not* a
    permission: the token carries no role claim, and `IsOwnerAdmin` re-reads the
    row on every request. A client that tampered with its stored copy would gain
    a menu item that 403s. See the comment on `AdminUser.role`.

    `username` is retained, unchanged, because the storefront's existing admin
    screen reads it and knows nothing about any of the rest.

    `session_started_at` is passed by the refresh path and omitted by login, so
    a renewed token keeps the original session's clock rather than resetting it.
    """
    return Response(
        {
            "access_token": create_access_token(
                admin.email,
                version=admin.token_version,
                session_started_at=session_started_at,
            ),
            "token_type": "bearer",
            "username": admin.email,
            "email": admin.email,
            "name": admin.name,
            "role": admin.role,
        }
    )


def _rider_token_response(rider: User, *, session_started_at=None) -> Response:
    return Response(
        {
            "access_token": create_access_token(
                rider.phone,
                token_type=RIDER_TOKEN,
                version=rider.token_version,
                session_started_at=session_started_at,
            ),
            "token_type": "bearer",
            # The full profile, because the app needs the rider's id and service
            # radius straight after login. UserSerializer omits `pin_hash`.
            "rider": UserSerializer(rider).data,
        }
    )


def renewable_session(request) -> tuple[Response | None, object]:
    """`(refusal, session_start)` for a refresh request.

    Returns a 401 response in the first slot when the session has been running
    longer than `SESSION_MAX_HOURS` and must not be renewed again, and otherwise
    the moment it began, to be copied into the replacement token.

    `request.auth` is the raw token the authentication class already accepted,
    so the session's start is read back out of it rather than tracked in a
    table. There is no session table here by design — the point of a bearer
    token is that the server holds nothing — and adding one to carry a single
    timestamp would put a database write on every request to save a decode.
    """
    # Every identity that can refresh a session needs a branch here, and the
    # failure mode when one is missing is quiet: the token decodes as the wrong
    # `typ`, `decode_token` returns None, and the refusal below signs the caller
    # out. Not on a bad token — on their *first refresh*, every time, which
    # looks like a session that will not stick rather than like a missing case.
    if isinstance(request.user, User):
        token_type = RIDER_TOKEN
    elif isinstance(request.user, Customer):
        token_type = CUSTOMER_TOKEN
    else:
        token_type = ADMIN_TOKEN
    claims = decode_token(request.auth, token_type)
    if claims is None:  # pragma: no cover — it authenticated moments ago
        return _session_expired(), None

    started = session_started(claims)
    if timezone.now() - started > timedelta(hours=settings.SESSION_MAX_HOURS):
        return _session_expired(), None
    return None, started


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

    The refresh is bounded. See `SESSION_MAX_HOURS`: without a ceiling this
    endpoint turns a twelve-hour token into a permanent one, renewable by
    whoever holds it.
    """

    permission_classes = [IsAdmin]

    @extend_schema(responses=LoginResponseSerializer)
    def get(self, request):
        refusal, started = renewable_session(request)
        if refusal is not None:
            return refusal
        return _token_response(request.user, session_started_at=started)


class LogoutView(APIView):
    """POST /api/auth/logout — retire every token this account holds.

    Returns 204 and is safe to call twice; each call simply moves the version on
    again. The client should clear its stored session whether this succeeds or
    fails, because a rider or manager who taps "sign out" on a phone with no
    signal must still be signed out of that phone.

    `F("token_version") + 1` rather than read-modify-write: two devices signing
    out at once would otherwise both read the same number and write the same
    one, and the second sign-out would be a no-op.
    """

    permission_classes = [IsAdmin]

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        AdminUser.objects.filter(pk=request.user.pk).update(
            token_version=F("token_version") + 1
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


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
        refusal, started = renewable_session(request)
        if refusal is not None:
            return refusal
        return _rider_token_response(request.user, session_started_at=started)


class RiderLogoutView(APIView):
    """POST /api/auth/rider/logout — the rider's half of LogoutView."""

    permission_classes = [IsRider]

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        User.objects.filter(pk=request.user.pk).update(
            token_version=F("token_version") + 1
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
def _phone_taken() -> Response:
    return Response(
        {"detail": "An account already exists for this number."},
        status=status.HTTP_409_CONFLICT,
    )


def _claim_order(customer: Customer, tracking_token: str) -> None:
    """Link one order the caller can prove they hold to their new account.

    Called only from sign-up, with the token of the order the customer just
    placed as a guest — otherwise they would create an account at checkout and
    immediately see an empty order list, which reads as the store having lost
    it.

    **Possession of the token is the evidence, not the phone number.** That is
    already the trust model of the public tracking endpoint, which shows a name,
    a number and an address to anyone holding one of these; linking the same
    order to an account grants nothing that was not already granted. The phone
    number proves nothing until an OTP says otherwise, which is why this claims
    exactly the one order named and never everything sharing a number.

    `customer__isnull=True` is the guard that matters: an order already
    belonging to somebody cannot be taken, so a leaked token cannot move an
    order between accounts.
    """
    token = (tracking_token or "").strip()
    if not token:
        return
    Order.objects.filter(tracking_token=token, customer__isnull=True).update(
        customer=customer
    )


def _customer_token_response(
    customer: Customer, *, session_started_at=None, status_code: int = status.HTTP_200_OK
) -> Response:
    """The storefront session payload. Third of three, same shape as the others."""
    return Response(
        {
            "access_token": create_access_token(
                customer.phone,
                token_type=CUSTOMER_TOKEN,
                version=customer.token_version,
                session_started_at=session_started_at,
            ),
            "token_type": "bearer",
            "customer": CustomerSerializer(customer).data,
        },
        status=status_code,
    )


class CustomerSignupView(APIView):
    """POST /api/auth/customer/signup — create an account and sign straight in.

    Public, and returns a token rather than asking the caller to sign in again:
    someone who has just chosen a password has proved enough for one session,
    and a sign-up that ends at a login form loses the people it was built for.

    **This endpoint tells a stranger whether a number is registered**, by
    answering 409, and that is worth stating because the rest of this module
    goes to real trouble not to — `LoginView` returns one message for an unknown
    email and a wrong password precisely so nobody can enumerate admins. A
    sign-up form has nowhere to hide the fact: "this number already has an
    account" is the only answer that lets the person do the right thing next,
    and the alternative — always 201, silently signing them into nothing — is
    unusable. The leak is bounded by the `customer_auth` throttle, and that a
    number shops at a grocery is not a secret worth an unusable form.
    """

    throttle_scope = "customer_auth"

    @extend_schema(
        request=CustomerSignupSerializer,
        responses={201: CustomerTokenResponseSerializer},
    )
    def post(self, request):
        serializer = CustomerSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            # **The `atomic()` is what makes the `except` survivable**, and it
            # is not optional. A failed statement marks its transaction for
            # rollback, so catching the error without a savepoint to rewind to
            # leaves the connection unusable and the *next* query — the claim
            # below, or anything a future ATOMIC_REQUESTS wrapped around the
            # view — fails with an error about an aborted transaction rather
            # than about a duplicate phone number. `checkout.place_order`
            # sidesteps the same trap by catching outside its atomic block;
            # there is no natural outer block here, so this makes one.
            with transaction.atomic():
                customer = Customer.objects.create(
                    phone=data["phone"],
                    password_hash=hash_password(data["password"]),
                    name=data.get("name", ""),
                    last_login_at=timezone.now(),
                )
        except IntegrityError:
            # Two sign-ups for one number, racing. `phone` is unique, so the
            # database settles it and the loser gets the answer they would have
            # got a millisecond earlier. Checking first and inserting after
            # would leave a window; letting the constraint decide has none.
            return _phone_taken()

        _claim_order(customer, data.get("claim_token", ""))

        # Not audited. `api/audit.py` records what *staff* did and is read
        # behind IsOwnerAdmin; a hundred sign-ups a day would bury the handful
        # of entries it exists to surface. A log line instead — carrying the id
        # and not the number, because that is PII in Cloud Logging with no
        # retention story.
        logger.info("customer signed up", extra={"customer_id": customer.pk})
        return _customer_token_response(customer, status_code=status.HTTP_201_CREATED)


class CustomerLoginView(APIView):
    """POST /api/auth/customer/login — phone + password.

    Its own throttle scope rather than the `login` one the two staff routes
    share. Both key on the IP address, because neither request carries a token
    yet, so one bucket would let a shopper mistyping their password on a carrier
    NAT stop a rider signing in to start a shift.
    """

    throttle_scope = "customer_auth"

    @extend_schema(
        request=CustomerLoginSerializer, responses=CustomerTokenResponseSerializer
    )
    def post(self, request):
        serializer = CustomerLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data["phone"]
        password = serializer.validated_data["password"]

        customer = Customer.objects.filter(phone=phone, is_active=True).first()

        # One message for an unknown number, a wrong password and a deactivated
        # account, so the response cannot be used to discover who shops here.
        # (The timing side-channel `RiderLoginView` documents applies equally:
        # an unknown number skips the PBKDF2 verify and answers sooner. Closing
        # it means hashing against a dummy digest on every miss, which becomes
        # worth doing the day this list is worth harvesting.)
        if customer is None or not verify_password(password, customer.password_hash):
            return Response(
                {"detail": "Incorrect phone or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # update_fields so a sign-in cannot clobber a concurrent edit to the
        # name or to the verification stamp.
        customer.last_login_at = timezone.now()
        customer.save(update_fields=["last_login_at"])

        return _customer_token_response(customer)


class CustomerMeView(CustomerAPIView):
    """GET /api/auth/customer/me — validate a stored token, and refresh it.

    PATCH /api/auth/customer/me — change the one thing a customer may change
    about themselves. Not the phone: it is the account's identity, and moving it
    would carry whatever `phone_verified_at` claimed about the old number onto a
    new one.
    """

    @extend_schema(responses=CustomerTokenResponseSerializer)
    def get(self, request):
        refusal, started = renewable_session(request)
        if refusal is not None:
            return refusal
        return _customer_token_response(request.user, session_started_at=started)

    @extend_schema(request=CustomerProfileSerializer, responses=CustomerSerializer)
    def patch(self, request):
        serializer = CustomerProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        customer = request.user
        customer.name = serializer.validated_data["name"]
        customer.save(update_fields=["name"])
        return Response(CustomerSerializer(customer).data)


class CustomerLogoutView(CustomerAPIView):
    """POST /api/auth/customer/logout — the customer's half of LogoutView."""

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        Customer.objects.filter(pk=request.user.pk).update(
            token_version=F("token_version") + 1
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerPasswordView(CustomerAPIView):
    """POST /api/auth/customer/password — change it, knowing the current one.

    Requiring the current password is what stops a borrowed phone with an open
    session becoming a permanent takeover. The session alone is enough to place
    an order; it is deliberately not enough to lock the owner out.

    **Returns a token rather than 204.** The change bumps `token_version`, which
    retires every token the account holds — including the one that made this
    request. Handing back a fresh one leaves *this* device signed in and signs
    out every other, which is the entire point of changing a password because
    you think someone else has used your phone.
    """

    @extend_schema(
        request=CustomerPasswordSerializer, responses=CustomerTokenResponseSerializer
    )
    def post(self, request):
        customer = request.user
        serializer = CustomerPasswordSerializer(
            data=request.data, context={"customer": customer}
        )
        serializer.is_valid(raise_exception=True)

        if not verify_password(
            serializer.validated_data["current_password"], customer.password_hash
        ):
            # 401 rather than 400: the body was well-formed and the credential
            # was wrong. It does not end the session — the client shows this
            # against the field and the customer stays signed in.
            return Response(
                {"detail": "Incorrect password."}, status=status.HTTP_401_UNAUTHORIZED
            )

        customer.password_hash = hash_password(serializer.validated_data["new_password"])
        customer.token_version = F("token_version") + 1
        customer.save(update_fields=["password_hash", "token_version"])
        # `F()` leaves the in-memory attribute as an expression rather than a
        # number, and the replacement token has to carry the number. Without
        # this re-read every request made with the token we are about to hand
        # back fails its `ver` comparison — an immediate, total sign-out.
        customer.refresh_from_db()

        logger.info("customer password changed", extra={"customer_id": customer.pk})
        return _customer_token_response(customer)
