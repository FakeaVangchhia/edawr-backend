"""Rate limits for authenticated staff traffic.

DRF's `AnonRateThrottle` returns `None` — no limit at all — as soon as a request
carries credentials, and until now no admin or rider view set a throttle. So one
valid token was an unmetered channel into every endpoint, including
`GET /api/delivery/{id}/dashboard`, which the rider app polls every 15 seconds
and which walks every Ready order in Python. The console added by this change
polls harder still.

The subtlety is the cache key.
"""

from rest_framework.throttling import SimpleRateThrottle

from api.models import AdminUser, User


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
    """

    scope = "staff"

    def get_cache_key(self, request, view):
        user = request.user
        if isinstance(user, AdminUser):
            return self.cache_format % {"scope": self.scope, "ident": f"admin:{user.pk}"}
        if isinstance(user, User):
            return self.cache_format % {"scope": self.scope, "ident": f"staff:{user.pk}"}
        # Not a staff caller. AnonRateThrottle owns this request.
        return None
