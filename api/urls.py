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
    admins,
    analytics,
    audit,
    auth,
    categories,
    customer,
    delivery,
    meta,
    orders,
    products,
    reports,
    settings as store_settings,
    store,
    uploads,
    users,
)

urlpatterns = [
    # --- meta (public) ----------------------------------------------------
    path("api/health", meta.health, name="health"),
    path("api/health/ready", meta.readiness, name="readiness"),

    # --- failure reports (public) -----------------------------------------
    # Public because a crash report is worth having precisely when nobody is
    # signed in, and because the browser sends a CSP violation itself with no
    # way to attach a token. Both are throttled under the `reports` scope,
    # allowlist every field they log, and touch no database. See views/reports.py.
    path("api/client-errors", reports.ClientErrorView.as_view(), name="client-errors"),
    path("api/csp-report", reports.CspReportView.as_view(), name="csp-report"),

    # --- auth (public entry points) ---------------------------------------
    path("api/auth/login", auth.LoginView.as_view(), name="login"),
    path("api/auth/me", auth.MeView.as_view(), name="me"),
    # Guarded, not public: signing out retires the caller's own credentials, so
    # it has to know whose they are. It answers 204 and is safe to call twice.
    path("api/auth/logout", auth.LogoutView.as_view(), name="logout"),
    path("api/auth/rider/login", auth.RiderLoginView.as_view(), name="rider-login"),
    path("api/auth/rider/me", auth.RiderMeView.as_view(), name="rider-me"),
    path("api/auth/rider/logout", auth.RiderLogoutView.as_view(), name="rider-logout"),
    # Customer accounts. Sign-up and sign-in are public for the same reason the
    # two above are — they are how you get a token. The other three are guarded
    # and act on the caller's own account, which they read from the token and
    # never from the body.
    path(
        "api/auth/customer/signup",
        auth.CustomerSignupView.as_view(),
        name="customer-signup",
    ),
    path(
        "api/auth/customer/login",
        auth.CustomerLoginView.as_view(),
        name="customer-login",
    ),
    path("api/auth/customer/me", auth.CustomerMeView.as_view(), name="customer-me"),
    path(
        "api/auth/customer/logout",
        auth.CustomerLogoutView.as_view(),
        name="customer-logout",
    ),
    path(
        "api/auth/customer/password",
        auth.CustomerPasswordView.as_view(),
        name="customer-password",
    ),

    # --- customer account (signed in) -------------------------------------
    # The account's own data. No customer id appears in any of these paths:
    # the account comes from the token, the same rule the rider routes below
    # follow, so there is nothing to tamper with.
    path("api/customer/orders", customer.CustomerOrdersView.as_view(), name="customer-orders"),
    path(
        "api/customer/orders/claim",
        customer.CustomerOrderClaimView.as_view(),
        name="customer-order-claim",
    ),
    path(
        "api/customer/push-token",
        customer.CustomerDeviceView.as_view(),
        name="customer-push-token",
    ),

    # --- storefront (public) ----------------------------------------------
    # Everything a customer without an account can reach. Checkout and tracking
    # write and read order data with no token, so each is throttled and each is
    # keyed on something unguessable rather than on a sequential id.
    path("api/store/config", store.StoreConfigView.as_view(), name="store-config"),
    path("api/store/products", store.StoreProductListView.as_view(), name="store-products"),
    path(
        "api/store/products/<int:product_id>",
        store.StoreProductDetailView.as_view(),
        name="store-product-detail",
    ),
    path("api/store/categories", store.StoreCategoryListView.as_view(), name="store-categories"),
    path("api/store/quote", store.BasketQuoteView.as_view(), name="store-quote"),
    path("api/store/orders", store.CheckoutView.as_view(), name="store-checkout"),
    path("api/store/orders/<str:token>", store.OrderTrackingView.as_view(), name="store-order-track"),
    path("api/store/orders/<str:token>/cancel", store.OrderCancelView.as_view(), name="store-order-cancel"),
    # Where the rider is, and where the customer says they are waiting. Public
    # for the same reason the two routes above are: the tracking token is the
    # customer's only credential, and it is already enough to read this order's
    # name, address and phone number, so attaching a position to it grants
    # nothing new.
    #
    # The read is narrow on purpose — a point, a heading and a distance, and
    # only while the order is out for delivery. It is a separate route from the
    # tracking payload so that everything a customer may learn about a rider's
    # position lives in one small view. See api/views/store.py.
    path(
        "api/store/orders/<str:token>/rider-location",
        store.TrackedRiderLocationView.as_view(),
        name="store-order-rider-location",
    ),
    path(
        "api/store/orders/<str:token>/location",
        store.CustomerLocationView.as_view(),
        name="store-order-customer-location",
    ),

    # --- products (admin) -------------------------------------------------
    path("api/products", products.ProductListCreateView.as_view(), name="product-list"),
    path("api/products/<int:product_id>", products.ProductDetailView.as_view(), name="product-detail"),

    # --- categories (admin) -----------------------------------------------
    path("api/categories", categories.CategoryListCreateView.as_view(), name="category-list"),
    path("api/categories/<int:category_id>", categories.CategoryDetailView.as_view(), name="category-detail"),

    # --- users (admin) ----------------------------------------------------
    # Store staff: managers and riders. Operational records, not console logins.
    path("api/users", users.UserListCreateView.as_view(), name="user-list"),
    path("api/users/<int:user_id>", users.UserDetailView.as_view(), name="user-detail"),

    # --- console accounts (ADMIN role only) -------------------------------
    # The other user table: who may sign in to the console, and as what. Guarded
    # by IsOwnerAdmin rather than IsAdmin, so a Manager gets 403 here and 200 on
    # everything above. This is the whole of what the two roles differ by, along
    # with the audit log below.
    path("api/admins", admins.AdminListCreateView.as_view(), name="admin-list"),
    path("api/admins/<int:admin_id>", admins.AdminDetailView.as_view(), name="admin-detail"),

    # --- audit (ADMIN role only) ------------------------------------------
    path("api/audit", audit.AuditLogListView.as_view(), name="audit-list"),

    # --- store settings (admin console, either role) ----------------------
    # Opening hours, the pause switch and the delivery radius. Either role, on
    # purpose: these are how a Manager runs the store, and an Admin's extra
    # authority is over who runs it, not over when it opens.
    path("api/settings", store_settings.StoreSettingsView.as_view(), name="store-settings"),

    # --- analytics (admin console, either role) ---------------------------
    path("api/analytics/summary", analytics.AnalyticsSummaryView.as_view(), name="analytics-summary"),
    path("api/analytics/revenue", analytics.RevenueSeriesView.as_view(), name="analytics-revenue"),
    path("api/analytics/products", analytics.TopProductsView.as_view(), name="analytics-products"),
    path("api/analytics/categories", analytics.CategoryShareView.as_view(), name="analytics-categories"),
    path("api/analytics/delivery", analytics.DeliveryPerformanceView.as_view(), name="analytics-delivery"),
    path("api/analytics/inventory", analytics.InventoryHealthView.as_view(), name="analytics-inventory"),
    # The till. Buckets by when the cash arrived rather than when the order
    # was placed — see the docstring on `collected_orders`.
    path("api/analytics/cash", analytics.CashReconciliationView.as_view(), name="analytics-cash"),

    # --- orders (mixed access — see views/orders.py) ----------------------
    path("api/orders", orders.OrderListView.as_view(), name="order-list"),
    path("api/orders/<int:order_id>/assign", orders.OrderAssignView.as_view(), name="order-assign"),
    path("api/orders/<int:order_id>/status", orders.OrderStatusView.as_view(), name="order-status"),
    path("api/orders/<int:order_id>/accept", orders.OrderAcceptView.as_view(), name="order-accept"),
    path("api/orders/<int:order_id>/reject", orders.OrderRejectView.as_view(), name="order-reject"),
    # The second half of a failed delivery: the goods are back on the shelf.
    # Separate from the status change because the two happen at different times
    # and only one of them moves stock.
    path("api/orders/<int:order_id>/restock", orders.OrderRestockView.as_view(), name="order-restock"),

    # --- delivery / rider app --------------------------------------------
    # Literal path before the parameterised one. Not strictly required, since
    # <int:> will not match "riders" or "availability", but it is the habit to
    # keep: the day one of these becomes <str:>, the order is what saves you.
    path("api/delivery/riders", delivery.RiderListView.as_view(), name="rider-list"),
    path("api/delivery/availability", delivery.RiderAvailabilityView.as_view(), name="rider-availability"),
    # Where to buzz this rider. POST registers a handset, DELETE forgets it at
    # sign-out. Guarded, not public: the token identifies a phone we will send
    # order addresses to, and the rider it belongs to comes from the bearer
    # token rather than the body. See api/push.py.
    path("api/delivery/push-token", delivery.RiderDeviceView.as_view(), name="rider-push-token"),
    # Live position. POST is the rider's own handset reporting where it is —
    # no rider id in the path or the body, because the rider is the token, the
    # same rule accept/reject/status follow. GET is the console's map and is
    # admin-only. Plural vs singular is the difference: one rider writes one
    # position, a manager reads all of them.
    path("api/delivery/location", delivery.RiderLocationReportView.as_view(), name="rider-location-report"),
    path("api/delivery/locations", delivery.RiderLocationListView.as_view(), name="rider-location-list"),
    path("api/delivery/<int:delivery_id>/dashboard", delivery.RiderDashboardView.as_view(), name="rider-dashboard"),

    # --- uploads (admin) --------------------------------------------------
    path("api/uploads/products/image", uploads.ProductImageUploadView.as_view(), name="product-image-upload"),
]
