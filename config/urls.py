"""Root URL configuration — the top of the routing tree.

This is the direct replacement for the `app.include_router(...)` block in the
old `app/main.py`, and the shift is worth naming:

    FastAPI:  the route lives with the handler (`@router.get("/{id}")` sits
              directly above the function), and routers are attached to the app.
    Django:   the route lives in a URLconf, separate from the view. A URL points
              *at* a view by reference.

The cost is one extra file to keep in step. The benefit is that this file is a
complete, readable table of contents for the API — you can see every path the
server answers without opening a single view.

`include()` splices another URLconf in under a prefix, which is how `api/urls.py`
owns everything under `/api/`.
"""

from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    # Everything the frontend and mobile app call.
    path("", include("api.urls")),

    # Interactive API docs, same address as FastAPI's: http://localhost:8000/docs
    # FastAPI generated the OpenAPI schema from your type hints; drf-spectacular
    # generates it from your serializers and view classes.
    path("api/schema", SpectacularAPIView.as_view(), name="schema"),
    path("docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),

    # Uploaded product images, served at /uploads/<filename> — the same path the
    # StaticFiles mount used, so `image_url` values in the database still work.
    #
    # Django normally serves media only when DEBUG is on (the usual advice is to
    # put nginx or object storage in front in production). This serves it
    # unconditionally so images do not silently vanish the first time you set
    # ENVIRONMENT=production locally; swap it for a real file server when you
    # actually deploy.
    re_path(
        r"^uploads/(?P<path>.*)$",
        serve,
        {"document_root": settings.MEDIA_ROOT},
        name="media",
    ),
]
