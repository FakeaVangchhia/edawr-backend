"""Admin product CRUD.

**Read this file first if you are learning DRF.** It exercises every piece:
URL-to-view binding, a serializer used for both input and output, permissions,
path parameters, status codes, and a hand-written conflict check.

The shape to notice: in FastAPI one *function* handled one method on one path,
and the decorator carried the URL. In DRF one *class* handles every method on
one path, and the URL lives in `api/urls.py` pointing at `Class.as_view()`.
A class per URL, a method per HTTP verb:

    class ProductListCreateView:    def get()  -> GET  /api/products
                                    def post() -> POST /api/products

`AdminAPIView` (api/permissions.py) is just `APIView` with
`permission_classes = [IsAdmin]`. Subclassing it is how this module gets the
guard that `APIRouter(dependencies=[Depends(require_admin)])` used to provide:
attached once, applying to every method, including ones added later.
"""

from django.db import transaction
from django.db.models import F, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api import audit
from api.models import STATUS_CHOICES, AuditLog, OrderItem, Product
from api.paging import read_page
from api.permissions import AdminAPIView
from api.serializers import ProductSerializer, SuccessSerializer
from api.views.uploads import delete_stored_image

# The two words `?status=` will accept. Shared with Category, which uses the
# same vocabulary.
CATALOGUE_STATUSES = [value for value, _ in STATUS_CHOICES]


def get_product(product_id: int) -> Product:
    """Fetch or 404, so every view in this file fails identically.

    `NotFound` is a DRF exception, so raising it produces
    `{"detail": "Product not found."}` with a 404 — the same contract
    `raise HTTPException(status_code=404, detail=...)` gave you.
    """
    product = Product.objects.filter(pk=product_id).first()
    if product is None:
        raise NotFound("Product not found.")
    return product


# The columns worth showing in an audit diff. Deliberately not every field —
# a log entry that lists eighteen unchanged values buries the one that moved.
AUDITED_FIELDS = (
    "name", "sku", "category", "brand", "unit", "price", "cost_price", "mrp",
    "stock", "reorder_level", "status", "supplier_name", "image_url",
)


def _snapshot(product: Product) -> dict:
    return {field: getattr(product, field) for field in AUDITED_FIELDS}


class ProductListCreateView(AdminAPIView):
    # `@extend_schema` is how a plain APIView tells drf-spectacular what it
    # takes and returns. A `generics`/ViewSet view would be introspected
    # automatically from `serializer_class`; the trade for writing views
    # explicitly is that you also declare their schema explicitly. Without it
    # the endpoint still works, but /docs shows an empty body.
    @extend_schema(responses=ProductSerializer(many=True))
    def get(self, request):
        """GET /api/products?q=&category=&status=&stock=low|out&limit=&offset=

        This used to return the entire table with no filter and no limit, which
        was fine while the catalogue was a seed script and is not fine once a
        store builds a real one. The console needs to search it, so the search
        belongs here rather than in the browser: filtering ten thousand products
        client-side means shipping ten thousand products to do it.

        The response stays a **bare JSON array** — no `{count, results}`
        envelope, matching every other list endpoint in this API. The total goes
        in `X-Total-Count`, so the console can page without three clients having
        to relearn the body shape.
        """
        products = Product.objects.order_by("id")

        query = (request.query_params.get("q") or "").strip()
        if query:
            products = products.filter(
                Q(name__icontains=query)
                | Q(sku__icontains=query)
                | Q(brand__icontains=query)
                | Q(category__icontains=query)
            )

        category = (request.query_params.get("category") or "").strip()
        if category:
            products = products.filter(category__iexact=category)

        state = (request.query_params.get("status") or "").strip().lower()
        if state:
            # Same reasoning as `?status=` on the order list: an unrecognised
            # value silently filtered to nothing, so "actve" reported an empty
            # catalogue rather than a typo.
            if state not in CATALOGUE_STATUSES:
                raise ValidationError(
                    f"Unknown status '{state}'. Expected one of: "
                    + ", ".join(CATALOGUE_STATUSES)
                    + "."
                )
            products = products.filter(status=state)

        # "Which shelves need walking" — the single most common reason a manager
        # opens this screen, so it is a filter rather than a client-side sort.
        stock = (request.query_params.get("stock") or "").strip().lower()
        if stock == "out":
            products = products.filter(stock__lte=0)
        elif stock == "low":
            products = products.filter(stock__gt=0, stock__lte=F("reorder_level"))

        total = products.count()
        limit, offset = read_page(request, default=50, maximum=200)
        page = products[offset : offset + limit]

        response = Response(ProductSerializer(page, many=True).data)
        response["X-Total-Count"] = str(total)
        return response

    @extend_schema(request=ProductSerializer, responses={201: ProductSerializer})
    def post(self, request):
        """POST /api/products

        The three-line shape you will write over and over in DRF:

            serializer = XSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()

        `request.data` is the parsed body regardless of content type — JSON,
        form-encoded or multipart. `.save()` inserts the row and returns the
        model instance, and `serializer.data` then renders it back out with the
        database-assigned `id` and `created_at` filled in.
        """
        serializer = ProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = serializer.save()
        audit.record(
            request, AuditLog.CREATE, "product", product.pk,
            f"Created product {product.name}",
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ProductDetailView(AdminAPIView):
    """Read, update and delete one product.

    **There is deliberately no PUT here**, and products are the only resource in
    this API that lacks one. `PUT /api/categories/{id}` and
    `PUT /api/users/{id}` both remain, because the distinction is a real one:

    A PUT is a full-row replace, written from a body the client assembled when
    it opened the editor. That is harmless for a category — nothing else in the
    system writes a category's name while a manager is looking at it. `Product`
    carries `stock`, which `api/checkout.py` decrements under a row lock on
    every order. So a manager who opened a product at stock 20, sold two while
    the form sat open, and then saved, wrote 20 back and put two sold units back
    on the shelf. The lock in `checkout.py` defends checkouts from each other;
    it cannot defend against a full-row UPDATE arriving from the console.

    Locking this method would have made that overwrite atomic without making it
    correct — the stale number still wins, just tidily. The bug is the replace
    semantics, not the race, so the method is gone and PATCH below is the only
    write path. Nothing called it: the storefront's old `/admin` screen was
    deleted in the frontend rebuild, and the console's `replaceProduct` helper
    was never wired to a button (and has since been removed too).

    `product_id` arrives as a keyword argument because `api/urls.py` declares
    the path as `api/products/<int:product_id>`. The `int:` converter both
    validates and casts, so `/api/products/abc` 404s before any code here runs.
    """

    @extend_schema(request=ProductSerializer, responses=ProductSerializer)
    def patch(self, request, product_id: int):
        """PATCH /api/products/{product_id} — change only what was sent.

        **This exists because a full replace loses concurrent stock
        decrements.** PUT wrote every column from a body the client assembled
        when it opened the editor: a manager who opens a product at stock 20,
        sells two while the form is open, then saves, wrote 20 back and put the
        two sold units back on the shelf. `checkout.py` locks product rows
        against *other checkouts*; it cannot defend against a full-row UPDATE
        arriving from the console. That method has since been removed rather
        than locked, because locking it would have made the overwrite atomic
        without making it correct.

        Two things fix it, and both are needed:

        - `select_for_update()` inside a transaction, so a checkout cannot
          decrement between this read and this write. Ordered by nothing here
          because it is a single row — the primary-key ordering rule in
          `checkout.py` matters when locking several.
        - `update_fields`, so the UPDATE names only the columns the caller
          actually sent. A field nobody edited is not written at all, and
          therefore cannot be written *back*.

        The console edits stock through this, and it is now the only write path
        for a product — see the class docstring for why PUT is gone.
        """
        with transaction.atomic():
            product = Product.objects.select_for_update().filter(pk=product_id).first()
            if product is None:
                raise NotFound("Product not found.")

            before = _snapshot(product)
            serializer = ProductSerializer(product, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)

            # Applied by hand rather than through `serializer.save()`, and that
            # is the crux of this method. `ModelSerializer.update()` ends in a
            # bare `instance.save()`, which writes *every* column — so calling it
            # and then re-saving with `update_fields` would still have clobbered
            # stock on the first write. `validated_data` has already dropped
            # anything unknown, so the field list cannot be widened by a caller
            # sending extra keys.
            #
            # ProductSerializer declares no custom `update()`; if it ever does,
            # this has to call it instead.
            touched = list(serializer.validated_data.keys())
            if not touched:
                return Response(ProductSerializer(product).data)

            # Remembered before the overwrite so a replaced picture can be
            # removed from disk after the row is safely written. Uploaded images
            # were never deleted by anything, so every re-crop of a product
            # photo left the previous one behind forever.
            replaced_image = (
                before["image_url"]
                if "image_url" in serializer.validated_data
                and serializer.validated_data["image_url"] != before["image_url"]
                else None
            )

            for field, value in serializer.validated_data.items():
                setattr(product, field, value)
            product.save(update_fields=touched)

            changes = audit.diff(before, _snapshot(product))
            if changes:
                audit.record(
                    request, AuditLog.UPDATE, "product", product.pk,
                    f"Updated {product.name} ({', '.join(sorted(changes))})",
                    changes,
                )

        # Outside the transaction on purpose. An unlinked file cannot be rolled
        # back, so it must not happen until the row that stopped referencing it
        # is committed.
        delete_stored_image(replaced_image)
        return Response(ProductSerializer(product).data)

    @extend_schema(responses={200: SuccessSerializer, 409: SuccessSerializer})
    def delete(self, request, product_id: int):
        """DELETE /api/products/{product_id}

        Refuses to delete a product that appears in any order, so order history
        is never rewritten. The caller is told to deactivate it instead, which
        hides it from the storefront (see views/store.py) while keeping the
        record intact.

        The model's `on_delete=models.PROTECT` would also stop this, but it
        raises `ProtectedError`, which surfaces as a 500. Checking first turns
        it into a 409 that says what to do instead.

        Returns `{"success": true}` because that is what the frontend expects.
        A stricter API would return 204 with an empty body.
        """
        product = get_product(product_id)

        order_count = OrderItem.objects.filter(product_id=product_id).count()
        if order_count:
            return Response(
                {
                    "detail": (
                        f"This product appears in {order_count} order(s) and cannot be "
                        'deleted. Set its status to "inactive" to hide it from the '
                        "storefront instead."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        name = product.name
        image_url = product.image_url
        product.delete()
        delete_stored_image(image_url)
        audit.record(
            request, AuditLog.DELETE, "product", product_id,
            f"Deleted product {name}",
        )
        return Response({"success": True})
