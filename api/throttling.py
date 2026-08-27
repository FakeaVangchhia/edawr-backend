"""Rate limits for authenticated traffic.

DRF's `AnonRateThrottle` returns `None` — no limit at all — as soon as a request
carries credentials, and until now no admin or rider view set a throttle. So one
valid token was an unmetered channel into every endpoint, including
`GET /api/delivery/{id}/dashboard`, which the rider app polls every 15 seconds
and which walks every Ready order in Python. The console added by this change
polls harder still.

**That hole reopens every time a new kind of authenticated caller is added**,
because the hole is in the *default* class list rather than in any one view.
Customer accounts were the second time: `AnonRateThrottle` stepped aside for
them and `StaffRateThrottle` did not recognise them. A fifth identity will need
a fifth class here, and adding one without it is silent — nothing errors, the
limits simply stop existing for that caller.

The subtlety is the cache key, and it is the same subtlety twice: every model
that can be `request.user` has its own primary-key sequence, so a key built from
`pk` alone merges people from different tables. `throttle_ident` is the one
place that knows how to name a caller, and all three classes below defer to it.
"""

from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from api.models import AdminUser, Customer, User


def throttle_ident(request) -> str:
    """Who this request is, for rate-limiting purposes, namespaced by table.

    Three models can be `request.user` here, each with its own primary-key
    sequence, so admin #3, rider #3 and customer #3 all exist and are three
    different people. Any throttle that keys on `pk` alone puts them in one
    bucket — a busy rider would throttle the owner out of the console, and each
    limit would be a fraction of what it says on the tin.

    Returns `""` for an anonymous caller, which every caller below reads as
    "not mine, key on the address instead".
    """
    user = getattr(request, "user", None)
    if isinstance(user, AdminUser):
        return f"admin:{user.pk}"
    if isinstance(user, User):
        return f"staff:{user.pk}"
    if isinstance(user, Customer):
        return f"customer:{user.pk}"
    return ""


class NamespacedScopedRateThrottle(ScopedRateThrottle):
    """`ScopedRateThrottle`, with the cross-table collision taken out.

    DRF's version keys on a bare `request.user.pk` for any authenticated caller
    (`rest_framework/throttling.py`, `get_cache_key`). That is the same
    collision `StaffRateThrottle` below exists to avoid, and it reaches every
    scope at once: `tracking` and `checkout` are both public, so an admin and a
    customer who happen to share a primary key share a budget on them.

    Fixing it here rather than on each scope means a scope added later inherits
    the fix instead of having to remember it.

    Keying on the account is otherwise an improvement worth having: a signed-in
    customer's checkout budget becomes theirs rather than their IP's, so a
    family behind one carrier NAT stops sharing twelve orders an hour.
    """

    def get_cache_key(self, request, view):
        # `allow_request` has already read the scope off the view and returned
        # early if there was none, so `self.scope` is set by the time this runs.
        # The ident is the only thing worth overriding.
        return self.cache_format % {
            "scope": self.scope,
            "ident": throttle_ident(request) or self.get_ident(request),
        }


class StaffRateThrottle(SimpleRateThrottle):
    """A per-account limit for any authenticated staff caller.

    **The identity must be namespaced by table.** `AdminUser` and `User` are two
    separate models with two separate primary-key sequences, so admin #3 and
    rider #3 both exist and are different people. DRF's stock `UserRateThrottle`
    keys on `request.user.pk` alone, which would put those two in one bucket:
    a busy rider would throttle the owner out of the console, and the limit would
    be half of what it says on either account. Prefixing with the model name is
    the entire fix, and it is the reason this class exists rather than a bare
    `UserRateThrottle` subclass.

    Anonymous requests return `None` — no key, no limit — because `AnonRateThrottle`
    is still in `DEFAULT_THROTTLE_CLASSES` and already covers them. Throttling
    them twice would apply the stricter of two limits by accident.

    A signed-in *customer* also returns `None` here, and is covered by
    `CustomerRateThrottle` below rather than by this class. They are metered
    separately because they are a different population with a different usage
    shape, not because one is trusted more.
    """

    scope = "staff"

    def get_cache_key(self, request, view):
        ident = throttle_ident(request)
        if ident.startswith(("admin:", "staff:")):
            return self.cache_format % {"scope": self.scope, "ident": ident}
        # Anonymous, or a customer. Another throttle owns this request.
        return None


class CustomerRateThrottle(SimpleRateThrottle):
    """A per-account limit for a signed-in customer.

    **This class is what stops customer accounts opening an unmetered hole in
    the whole API.** DRF's `AnonRateThrottle` returns no key the moment a
    request is authenticated, and `StaffRateThrottle` returns none for anyone
    who is not staff. A `Customer` — whose `is_authenticated` is True, because
    DRF requires it — therefore falls through both, and without this class
    signing in would remove every rate limit from every endpoint that does not
    set an explicit scope. That is precisely the bug this module was written to
    fix, arriving a second time through a new door.

    **The rate is deliberately the same as `anon`.** Signing in must not change
    how much of this API you can consume: tighter and signing in is a downgrade
    that teaches customers to sign out, looser and an account is a way to buy
    capacity. It is not `staff`, which is generous because the console and the
    rider app both poll dashboards on a timer; a customer polls one order.
    """

    scope = "customer"

    def get_cache_key(self, request, view):
        ident = throttle_ident(request)
        if ident.startswith("customer:"):
            return self.cache_format % {"scope": self.scope, "ident": ident}
        return None
