"""Health checks.

Two endpoints, because orchestrators ask two different questions and answering
both with "ok" causes outages:

  **liveness** — is this process wedged and in need of a restart? It must not
  touch the database. If it did, a brief database blip would make every replica
  fail its liveness probe at once, and the orchestrator would respond by killing
  all of them — turning a recoverable dependency failure into a total outage.

  **readiness** — should this replica receive traffic *right now*? This one does
  check the database, because a process that cannot reach it will fail every
  request it is handed. Failing readiness takes the replica out of the load
  balancer without restarting it, and it rejoins by itself when the dependency
  recovers.
"""

from django.core.cache import cache
from django.db import connection
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
def health(request):
    """GET /api/health — liveness. Deliberately checks nothing external."""
    return Response({"status": "ok"})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
def readiness(request):
    """GET /api/health/ready — readiness. 503 when a dependency is down.

    Returns the per-dependency detail rather than a bare boolean, so whoever is
    paged can see which one failed without shelling into the container.
    """
    checks: dict[str, str] = {}
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 — the reason is the payload
        checks["database"] = f"error: {exc.__class__.__name__}"
        healthy = False

    # The cache holds throttle counters. Losing it does not stop requests being
    # served, so this is reported but does not fail the probe — pulling every
    # replica out of the load balancer because Redis restarted would be a
    # bigger outage than the degraded rate limiting it is meant to prevent.
    try:
        cache.set("healthcheck", "1", 5)
        checks["cache"] = "ok" if cache.get("healthcheck") == "1" else "degraded"
    except Exception as exc:  # noqa: BLE001
        checks["cache"] = f"error: {exc.__class__.__name__}"

    return Response(
        {"status": "ok" if healthy else "unavailable", "checks": checks},
        status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
