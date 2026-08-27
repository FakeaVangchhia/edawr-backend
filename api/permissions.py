"""May this request proceed?

`IsAdmin` is the DRF replacement for FastAPI's `Depends(require_admin)`, and the
two attachment styles map one-for-one:

    FastAPI                                       DRF
    ------------------------------------------    -------------------------------
    APIRouter(dependencies=[Depends(...)])   ->   permission_classes on a base
                                                  class the views inherit from
    def route(admin = Depends(require_admin))->   permission_classes = [IsAdmin]
                                                  on the individual view class

Both keep the guard in the *declaration* rather than buried in a function body,
which was the whole point of moving off the old `requireAdmin(request)` calls:
forgetting it is a visible omission, not an invisible one.
"""

from rest_framework import permissions
from rest_framework.views import APIView

from api.models import AdminUser, Customer, User
from api.throttling import StaffRateThrottle


class IsAdmin(permissions.BasePermission):
    """Allow only requests carrying a valid admin bearer token.

    `request.user` is whatever an authentication class returned, or None (see
    the UNAUTHENTICATED_USER setting). A permission class returns a bool; DRF
    turns False into 401 when an authentication class advertises a
    `WWW-Authenticate` header, and 403 otherwise.

    The `isinstance` check is load-bearing now that a second scheme exists: a
    rider is also a truthy `request.user`, and admin endpoints must not accept
    one. Testing the *type* rather than an attribute means a field added to
    `User` later can never accidentally satisfy this.
    """

    message = "Not authenticated."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return isinstance(user, AdminUser) and user.is_active


class IsOwnerAdmin(permissions.BasePermission):
    """Allow only the ADMIN role. `IsAdmin` above lets either role through.

    The split is the whole point of two roles: a Manager runs the store — every
    product, order, rider, price and setting — while an Admin additionally
    decides *who runs the store*, and can read the record of what they did. So
    exactly two surfaces sit behind this class: `/api/admins` and `/api/audit`.

    **The role is read from the row, not from the token.** `request.user` was
    loaded by `AdminJWTAuthentication.resolve()` on this request, so demoting an
    account takes effect immediately — the same property that makes `is_active`
    a real revocation rather than a 12-hour delay.

    A Manager reaching one of these gets **403, not 401**. That distinction is
    load-bearing: 401 means "I don't know who you are" and is what makes a client
    clear its stored session. A Manager who clicks an Admin-only link is
    perfectly well known and must be told no, not signed out mid-shift.
    Returning False here yields 403 because the user *is* authenticated.
    """

    message = "This action requires an Admin account."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return isinstance(user, AdminUser) and user.is_active and user.is_owner_admin


class IsRider(permissions.BasePermission):
    """Allow only requests carrying a valid rider bearer token."""

    message = "Not authenticated."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return isinstance(user, User) and user.role == User.DELIVERY and user.is_active


class IsCustomer(permissions.BasePermission):
    """Allow only requests carrying a valid customer bearer token.

    Mirrors `IsRider`. Note what it does *not* check: `phone_verified_at`.
    Verification decides which orders a customer may see, not whether they may
    sign in — an unverified account is a real account, and gating the whole
    surface on a check nothing can currently satisfy would make every account
    useless. The gate lives in the queryset in `api/views/customer.py`, where it
    is a `WHERE` clause rather than a locked door.
    """

    message = "Not authenticated."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return isinstance(user, Customer) and user.is_active


class IsAdminOrRider(permissions.BasePermission):
    """Either staff token gets in; the *view* decides what each may do.

    Used by the status endpoint, which both a manager and a rider legitimately
    call — but for different transitions, and the rider only on their own
    orders. Splitting it into two URLs would mean two clients calling two paths
    to do one thing; putting the role logic in the view keeps one endpoint whose
    rules are written out in one place.
    """

    message = "Not authenticated."

    def has_permission(self, request, view) -> bool:
        return IsAdmin().has_permission(request, view) or IsRider().has_permission(
            request, view
        )


class AdminAPIView(APIView):
    """Base class for views where *every* method is admin-only.

    Subclassing this is the equivalent of putting the guard on the router:
    a method added to the subclass later is protected automatically, because the
    protection is not written per-method.

    Used by the products, categories, users and uploads views. `orders.py` does
    not use it — that file mixes admin and public endpoints on purpose, so its
    views set `permission_classes` individually and the bare ones stand out.
    """

    permission_classes = [IsAdmin]
    throttle_classes = [StaffRateThrottle]


class OwnerAdminAPIView(APIView):
    """Base class for views where every method requires the ADMIN role.

    Mirrors `AdminAPIView` deliberately, including the reason for existing: the
    guard lives in the declaration, so a method added to the subclass later is
    protected without anyone remembering to protect it. Used by `admins.py` and
    `audit.py`.
    """

    permission_classes = [IsOwnerAdmin]
    throttle_classes = [StaffRateThrottle]


class CustomerAPIView(APIView):
    """Base class for views where every method requires a signed-in customer.

    **Deliberately does not set `throttle_classes`**, unlike the two classes
    above. Setting it *replaces* the default list rather than adding to it, so
    these views would silently lose `NamespacedScopedRateThrottle` — and
    `settings.py` already records the lesson that a throttle you have to
    remember on each view is one that gets forgotten. `CustomerRateThrottle` is
    in the default list and covers every method here without being named.

    The two staff classes keep their explicit `throttle_classes` because they
    predate that reasoning and removing it now would change limits on live
    endpoints for no gain; this is the shape to copy for anything new.
    """

    permission_classes = [IsCustomer]
