"""Liveness check."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
def health(request):
    """GET /api/health — handy for confirming the server is actually up.

    `@api_view` is the function-based tier of DRF: it wraps a plain function so
    it gets DRF's request/response objects, content negotiation and exception
    handling. Every other view here is a class, which is the more common style
    once a URL needs more than one method — but for a one-line endpoint the
    decorator is less ceremony.
    """
    return Response({"status": "ok"})
