"""Root URL configuration — the top of the routing tree.

`include()` splices another URLconf in under a prefix, which is how
`api/urls.py` owns everything under `/api/`.

Two things here are conditional on configuration rather than always mounted,
and both defaults are the safe ones:

  **/docs and /api/schema** publish a complete, interactive map of the API. That
  is exactly what you want in development and exactly what you would rather not
  hand a stranger in production, so they follow SERVE_API_DOCS (default: on in
  development only).

  **/uploads/** is served by Django's own static file server, which is
  single-threaded, does no caching and supports no range requests. Fine for
  development, wasteful in production where nginx or object storage should sit
  in front. SERVE_MEDIA defaults to DEBUG; set it true only for a single-host
  deployment that knowingly accepts the cost.
"""

from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    # Everything the storefront, admin console and mobile app call.
    path("", include("api.urls")),
]

# Django resolves these by name, and only when DEBUG is off. Without them a
# 404 on a mistyped URL, or any exception DRF does not recognise, returns
# Django's HTML error page -- the one shape none of the three clients can read.
# See config/handlers.py.
handler400 = "config.handlers.bad_request"
handler403 = "config.handlers.permission_denied"
handler404 = "config.handlers.not_found"
handler500 = "config.handlers.server_error"

if settings.SERVE_API_DOCS:
    urlpatterns += [
        path("api/schema", SpectacularAPIView.as_view(), name="schema"),
        path("docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    ]

if settings.SERVE_MEDIA:
    urlpatterns += [
        re_path(
            r"^uploads/(?P<path>.*)$",
            serve,
            {"document_root": settings.MEDIA_ROOT},
            name="media",
        ),
    ]
