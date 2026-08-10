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

from api.models import AdminUser, User


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


class IsRider(permissions.BasePermission):
    """Allow only requests carrying a valid rider bearer token."""

    message = "Not authenticated."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return isinstance(user, User) and user.role == User.DELIVERY and user.is_active


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
