"""Every URL this API answers, in one table.

    FastAPI:  the URL is a decorator on the handler. Path and code are together;
              there is no list of routes anywhere.
    Django:   the URL is an entry in a URLconf that points at a view. Path and
              code are apart; this list *is* the routing table.

The cost is one extra file to keep in step. The benefit is that this file is a
complete, readable table of contents for the API — including, crucially, which
endpoints are public. The `(public)` markers below are the fastest way to audit
that, and every one of them should make you ask "should it be?".

**Path converters** replace FastAPI's type-annotated path parameters:
`<int:product_id>` matches digits only and casts to int, so `/api/products/abc`
404s before any view runs.

**No trailing slashes.** The frontend and Expo app call `/api/products`, so the
patterns are written without one and `APPEND_SLASH` is off in settings. A
redirect from the wrong form would drop POST bodies.
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
    # --- meta (public) ----------------------------------------------------
    path("api/health", meta.health, name="health"),
    path("api/health/ready", meta.readiness, name="readiness"),

    # --- auth (public entry points) ---------------------------------------
    path("api/auth/login", auth.LoginView.as_view(), name="login"),
    path("api/auth/me", auth.MeView.as_view(), name="me"),
    path("api/auth/rider/login", auth.RiderLoginView.as_view(), name="rider-login"),
    path("api/auth/rider/me", auth.RiderMeView.as_view(), name="rider-me"),

    # --- storefront (public) ----------------------------------------------
    # Everything a customer without an account can reach. Checkout and tracking
    # write and read order data with no token, so each is throttled and each is
    # keyed on something unguessable rather than on a sequential id.
    path("api/store/config", store.StoreConfigView.as_view(), name="store-config"),
    path("api/store/products", store.StoreProductListView.as_view(), name="store-products"),
    path("api/store/categories", store.StoreCategoryListView.as_view(), name="store-categories"),
    path("api/store/quote", store.BasketQuoteView.as_view(), name="store-quote"),
    path("api/store/orders", store.CheckoutView.as_view(), name="store-checkout"),
    path("api/store/orders/<str:token>", store.OrderTrackingView.as_view(), name="store-order-track"),
    path("api/store/orders/<str:token>/cancel", store.OrderCancelView.as_view(), name="store-order-cancel"),

    # --- products (admin) -------------------------------------------------
    path("api/products", products.ProductListCreateView.as_view(), name="product-list"),
    path("api/products/<int:product_id>", products.ProductDetailView.as_view(), name="product-detail"),

    # --- categories (admin) -----------------------------------------------
    path("api/categories", categories.CategoryListCreateView.as_view(), name="category-list"),
    path("api/categories/<int:category_id>", categories.CategoryDetailView.as_view(), name="category-detail"),

    # --- users (admin) ----------------------------------------------------
    path("api/users", users.UserListCreateView.as_view(), name="user-list"),
    path("api/users/<int:user_id>", users.UserDetailView.as_view(), name="user-detail"),

    # --- orders (mixed access — see views/orders.py) ----------------------
    path("api/orders", orders.OrderListView.as_view(), name="order-list"),
    path("api/orders/<int:order_id>/assign", orders.OrderAssignView.as_view(), name="order-assign"),
    path("api/orders/<int:order_id>/status", orders.OrderStatusView.as_view(), name="order-status"),
    path("api/orders/<int:order_id>/accept", orders.OrderAcceptView.as_view(), name="order-accept"),
    path("api/orders/<int:order_id>/reject", orders.OrderRejectView.as_view(), name="order-reject"),

    # --- delivery / rider app --------------------------------------------
    # Literal path before the parameterised one. Not strictly required, since
    # <int:> will not match "riders" or "availability", but it is the habit to
    # keep: the day one of these becomes <str:>, the order is what saves you.
    path("api/delivery/riders", delivery.RiderListView.as_view(), name="rider-list"),
    path("api/delivery/availability", delivery.RiderAvailabilityView.as_view(), name="rider-availability"),
    path("api/delivery/<int:delivery_id>/dashboard", delivery.RiderDashboardView.as_view(), name="rider-dashboard"),

    # --- uploads (admin) --------------------------------------------------
    path("api/uploads/products/image", uploads.ProductImageUploadView.as_view(), name="product-image-upload"),
]
