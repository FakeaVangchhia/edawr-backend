"""What a signed-in customer can see about themselves.

**Separate from `store.py` on purpose.** That module's docstring makes its own
boundary a security control: everything in it is reachable without a token, so
nothing in it may return a serializer carrying staff-only data. Putting an
authenticated endpoint in there would make the sentence false, and the next
person to add a public route would inherit a rule that no longer holds. The
auth endpoints themselves live in `auth.py`, beside their admin and rider
equivalents; what is left here is the account's own data.

Nothing in this module takes a customer id. The account comes from the token,
which is the same rule the rider endpoints follow — `accept`, `reject` and
`status` all read the rider from the token and each checks ownership — and it
means there is no id to tamper with in the first place.
"""

from __future__ import annotations

import logging

from django.conf import settings

from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status as http
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from django.utils import timezone

from api import otp, push, sms
from api.models import Order
from api.paging import read_page
from api.permissions import CustomerAPIView
from api.serializers import (
    CustomerClaimSerializer,
    CustomerSerializer,
    DeviceSerializer,
    OrderTrackingSerializer,
    PhoneChallengeResponseSerializer,
    PhoneVerifySerializer,
)
from api.views.store import TRACKED_ORDERS

logger = logging.getLogger(__name__)


def visible_orders(customer):
    """The orders this account is allowed to see, as one queryset.

    Two rules, and the second one is the whole verification design:

    1. **Orders linked to the account.** Placed while signed in, or claimed with
       a tracking token the customer was holding. Always visible.
    2. **Orders that merely carry the same phone number** — visible only once
       `phone_verified_at` is set.

    Rule 2 is why unverified accounts exist as a distinct state rather than as
    an inconvenience. Setting a password proves someone *knows* a number, not
    that they hold the SIM, so backfilling on the number alone would mean typing
    a stranger's number into the sign-up form and being handed their name, their
    delivery address and everything they have ever ordered. Nothing writes
    `phone_verified_at` yet — see the note on the model field — so today rule 2
    is dormant and rule 1 carries the feature.

    `customer__isnull=True` inside rule 2 is small and load-bearing: an order
    already belonging to somebody is never matched by a phone number. Indian
    mobile numbers are recycled after disconnection, so without it a new
    registrant of a reassigned number would eventually verify it and inherit the
    previous owner's history.
    """
    scope = Q(customer=customer)
    if customer.phone_verified_at is not None:
        scope |= Q(customer__isnull=True, customer_phone=customer.phone)
    # `-id` after `-created_at` because two orders placed in the same
    # millisecond otherwise have no stable order, and an unstable sort makes a
    # paged list drop and repeat rows between pages.
    return TRACKED_ORDERS.filter(scope).order_by("-created_at", "-id")


class CustomerOrdersView(CustomerAPIView):
    """GET /api/customer/orders — this account's order history.

    Serialised with `OrderTrackingSerializer`, unchanged and deliberately not a
    new class: it is already exactly the customer-visible projection of an order
    — no cost price, no rider identity, no dispatch distances — and it carries
    `tracking_token`, so the list can link straight to `/order/{token}` without
    a second request per row.

    There is no detail route to match. `/api/store/orders/{token}` already
    serves one order and the token is the credential; a signed-in variant would
    be a second path to the same bytes with its own chance to disagree.
    """

    @extend_schema(
        parameters=[
            OpenApiParameter("limit", int, description="Page size."),
            OpenApiParameter("offset", int, description="Rows to skip."),
        ],
        responses=OrderTrackingSerializer(many=True),
    )
    def get(self, request):
        orders = visible_orders(request.user)

        limit, offset = read_page(request, default=20, maximum=100)
        total = orders.count()

        response = Response(
            OrderTrackingSerializer(orders[offset : offset + limit], many=True).data
        )
        # A bare array plus the count in a header, matching every other list
        # endpoint here — the three clients all read that shape.
        response["X-Total-Count"] = str(total)
        return response


class CustomerOrderClaimView(CustomerAPIView):
    """POST /api/customer/orders/claim — attach an order I am holding to me.

    The counterpart of the `claim_token` on sign-up, for the customer who
    already had an account and placed an order before signing in — or who signed
    in on a new phone and pasted a link they still had.

    **Possession of the tracking token is the evidence, and it is sufficient.**
    That token is already the entire credential for `/api/store/orders/{token}`,
    which shows the customer's name, phone number and delivery address to
    whoever presents it. Letting the holder attach it to their account grants
    nothing that was not already granted, which is why this does not need — and
    must not wait for — a verified phone number.

    A 404 for an unknown token, and for one already claimed. Distinguishing the
    two would tell the holder of a random string whether it happened to be a
    real order.
    """

    @extend_schema(
        request=CustomerClaimSerializer, responses=OrderTrackingSerializer
    )
    def post(self, request):
        serializer = CustomerClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["tracking_token"]
        customer = request.user

        # `customer__isnull=True` in the filter rather than a check after the
        # read: two taps on a slow connection then settle in the database
        # instead of racing, and an order belonging to somebody else can never
        # be moved regardless of who holds the token.
        claimed = Order.objects.filter(
            tracking_token=token, customer__isnull=True
        ).update(customer=customer)

        if not claimed:
            already = Order.objects.filter(
                tracking_token=token, customer=customer
            ).first()
            if already is None:
                # Unknown, or somebody else's. One answer for both.
                raise NotFound("No such order.")
            # Already theirs. Idempotent: a second tap is a success, not a
            # conflict, because the end state the caller asked for is the state.
            return Response(OrderTrackingSerializer(already).data)

        logger.info(
            "customer claimed an order",
            extra={"customer_id": customer.pk},
        )
        return Response(
            OrderTrackingSerializer(TRACKED_ORDERS.get(tracking_token=token)).data
        )


class CustomerDeviceView(CustomerAPIView):
    """The customer app's push-notification registration. See `api/push.py`.

    POST registers this handset, DELETE forgets it. Both take the token in the
    body and the customer from their bearer token, so there is no account id to
    walk and nothing to spoof by editing a request - the rule the whole module
    follows.

    **Register on every launch, not once.** Expo rotates a push token whenever
    the app is reinstalled, restored to a new phone, or updated across certain
    native boundaries, and it never tells the server it did.
    `push.register_customer_device` upserts on the token, so the repeat is free
    and the row cannot drift out of date.

    **DELETE is what sign-out is for.** Expo delivers to a token, not to a
    session, so a handset left registered keeps being told about orders that are
    no longer this customer's. It is idempotent: forgetting a phone that is
    already forgotten is a 204, because the caller cannot know which it was and
    the outcome they wanted is the same either way.

    **This is the only route in the app that a guest cannot reach**, and the
    consequence is worth stating: a guest order sends no notifications and falls
    back to the tracking screen's poll. Keying devices on a tracking token
    instead would let anyone holding one subscribe somebody else's phone.
    """

    @extend_schema(request=DeviceSerializer, responses={204: None})
    def post(self, request):
        payload = DeviceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        push.register_customer_device(
            request.user,
            payload.validated_data["expo_token"],
            payload.validated_data.get("platform", ""),
        )
        # 204 rather than the row: the app has nothing to do with the id, and
        # echoing a credential-shaped value back is a habit worth not forming.
        return Response(status=http.HTTP_204_NO_CONTENT)

    @extend_schema(request=DeviceSerializer, responses={204: None})
    def delete(self, request):
        payload = DeviceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        push.forget_customer_device(request.user, payload.validated_data["expo_token"])
        return Response(status=http.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Phone verification
# --------------------------------------------------------------------------
class PhoneChallengeView(CustomerAPIView):
    """POST /api/customer/phone/challenge — text me a code.

    **The number comes from the token's row**, never from the body. A body that
    could name a number would be an endpoint that texts strangers, and — since a
    verified number is what unlocks unclaimed orders carrying it — one that
    verifies them too.

    Its own throttle scope (`otp`, ten an hour per account), and here that scope
    is doing the *other* half of its job: without it this is a free way to make
    somebody's phone buzz every few seconds, and the person being bothered is
    not the person being throttled.

    **503 when no provider is configured**, which is the state of every
    deployment today. Answering 200 and sending nothing would leave a customer
    watching a phone that was never going to ring, tapping resend into the same
    silence. See `api/sms.py`.
    """

    throttle_scope = "otp"

    @extend_schema(request=None, responses={200: PhoneChallengeResponseSerializer})
    def post(self, request):
        customer = request.user

        if customer.phone_verified_at is not None:
            # A conflict with the account's state, not a bad request — the same
            # distinction the order state machine draws.
            return Response(
                {"detail": "This number is already verified."},
                status=http.HTTP_409_CONFLICT,
            )

        code = otp.generate_code()
        try:
            sms.send_otp(customer.phone, code)
        except sms.SmsNotConfigured as exc:
            logger.warning("otp requested with no SMS backend configured")
            return Response(
                {"detail": str(exc)}, status=http.HTTP_503_SERVICE_UNAVAILABLE
            )

        # The code is not in this response and is not in the challenge: the
        # token carries an HMAC of it. See api/otp.py.
        return Response(
            {
                "challenge": otp.issue(customer.pk, customer.phone, code),
                "expires_in": settings.OTP_TTL_SECONDS,
                "phone": customer.phone,
            }
        )


class PhoneVerifyView(CustomerAPIView):
    """POST /api/customer/phone/verify — here is the code you texted me.

    Stamps `phone_verified_at`, which is the whole of this endpoint's job and
    the reason the model promised no migration would be needed. What it unlocks
    is one rule in `visible_orders` above: orders that merely *carry* this
    number and belong to nobody become visible. That is a stranger's order
    history if it is ever wrong, which is why the code has to prove possession
    of the SIM rather than knowledge of the number.
    """

    throttle_scope = "otp"

    @extend_schema(request=PhoneVerifySerializer, responses={200: CustomerSerializer})
    def post(self, request):
        payload = PhoneVerifySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        customer = request.user

        if customer.phone_verified_at is not None:
            return Response(CustomerSerializer(customer).data)

        try:
            otp.check(
                payload.validated_data["challenge"],
                payload.validated_data["code"],
                customer.pk,
                customer.phone,
            )
        except otp.InvalidChallenge:
            # One message for expired, tampered-with and simply wrong. Telling
            # them apart would tell an attacker which half of the guess to keep,
            # and none of the three is actionable differently by the customer.
            return Response(
                {"detail": "That code is wrong or has expired. Ask for a new one."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        customer.phone_verified_at = timezone.now()
        customer.save(update_fields=["phone_verified_at"])

        # A log line rather than an `AuditLog` row, following `customer signed
        # up` in auth.py: the audit trail records what *staff* did, and its
        # actor columns have no customer kind — this would file itself under
        # "system", which is worse than not filing it.
        logger.info("customer verified their phone", extra={"customer_id": customer.pk})

        return Response(CustomerSerializer(customer).data)
