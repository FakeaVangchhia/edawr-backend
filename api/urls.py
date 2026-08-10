"""Every URL this API answers, in one table.

This file is the piece with no FastAPI counterpart, and understanding *why* it
exists is most of the mental switch.

    FastAPI:  the URL is a decorator on the handler. Path and code are together;
              there is no list of routes anywhere.
    Django:   the URL is an entry in a URLconf that points at a view. Path and
              code are apart; this list *is* the routing table.

`path()` takes three things: a pattern, a view, and a `name` used for reverse
lookups (`reverse("product-detail", args=[7])`) so nothing else in the codebase
has to hardcode a URL string.

**Path converters** replace FastAPI's type-annotated path parameters:

    <int:product_id>   matches digits only, casts to int, passes it to the view
                       as the keyword argument `product_id`

A non-numeric segment does not match the pattern at all, so `/api/products/abc`
404s before any view runs. That is the same protection `product_id: int` gave
you, enforced one layer earlier.

**No trailing slashes.** Django's convention is `api/products/`, but the
frontend and Expo app call `/api/products`, so the patterns are written without
one and `APPEND_SLASH` is off in settings.py. Pick one and be consistent; a
redirect from the wrong form drops POST bodies.
"""

from django.urls import path

from api.views import (
    auth,
    categories,
    delivery,
    meta,
    orders,
    products,
    store,
    uploads,
    users,
)

urlpatterns = [
    # --- meta -----------------------------------------------------------
    path("api/health", meta.health, name="health"),

    # --- auth -----------------------------------------------------------
    path("api/auth/login", auth.LoginView.as_view(), name="login"),
    path("api/auth/me", auth.MeView.as_view(), name="me"),
    # Rider sign-in for the Expo app. Literal segments, so no ambiguity with the
    # admin routes above.
    path("api/auth/rider/login", auth.RiderLoginView.as_view(), name="rider-login"),
    path("api/auth/rider/me", auth.RiderMeView.as_view(), name="rider-me"),

    # --- storefront (public) --------------------------------------------
    path("api/store/products", store.StoreProductListView.as_view(), name="store-products"),

    # --- products (admin) -----------------------------------------------
    path("api/products", products.ProductListCreateView.as_view(), name="product-list"),
    path("api/products/<int:product_id>", products.ProductDetailView.as_view(), name="product-detail"),

    # --- categories (admin) ---------------------------------------------
    path("api/categories", categories.CategoryListCreateView.as_view(), name="category-list"),
    path("api/categories/<int:category_id>", categories.CategoryDetailView.as_view(), name="category-detail"),

    # --- users (admin) --------------------------------------------------
    path("api/users", users.UserListCreateView.as_view(), name="user-list"),

    # --- orders (mixed access — see views/orders.py) ---------------------
    path("api/orders", orders.OrderListView.as_view(), name="order-list"),
    path("api/orders/<int:order_id>/assign", orders.OrderAssignView.as_view(), name="order-assign"),
    path("api/orders/<int:order_id>/status", orders.OrderStatusView.as_view(), name="order-status"),
    path("api/orders/<int:order_id>/accept", orders.OrderAcceptView.as_view(), name="order-accept"),
    path("api/orders/<int:order_id>/reject", orders.OrderRejectView.as_view(), name="order-reject"),

    # --- delivery / rider app (public) -----------------------------------
    # Literal path first, parameterised path second. Not strictly required
    # because <int:> will not match "riders", but it is the habit to keep.
    path("api/delivery/riders", delivery.RiderListView.as_view(), name="rider-list"),
    path("api/delivery/<int:delivery_id>/dashboard", delivery.RiderDashboardView.as_view(), name="rider-dashboard"),

    # --- uploads (admin) --------------------------------------------------
    path("api/uploads/products/image", uploads.ProductImageUploadView.as_view(), name="product-image-upload"),
]
