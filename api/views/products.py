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

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api.models import OrderItem, Product
from api.permissions import AdminAPIView
from api.serializers import ProductSerializer, SuccessSerializer


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


class ProductListCreateView(AdminAPIView):
    # `@extend_schema` is how a plain APIView tells drf-spectacular what it
    # takes and returns. A `generics`/ViewSet view would be introspected
    # automatically from `serializer_class`; the trade for writing views
    # explicitly is that you also declare their schema explicitly. Without it
    # the endpoint still works, but /docs shows an empty body.
    @extend_schema(responses=ProductSerializer(many=True))
    def get(self, request):
        """GET /api/products — every product, oldest first."""
        products = Product.objects.order_by("id")
        # `many=True` serialises a queryset instead of one object. Passing the
        # queryset (not a list) lets DRF iterate it lazily.
        return Response(ProductSerializer(products, many=True).data)

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
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ProductDetailView(AdminAPIView):
    @extend_schema(request=ProductSerializer, responses=ProductSerializer)
    def put(self, request, product_id: int):
        """PUT /api/products/{product_id}

        `product_id` arrives as a keyword argument because `api/urls.py`
        declares the path as `api/products/<int:product_id>`. The `int:`
        converter both validates and casts: `/api/products/abc` never matches
        this route at all and 404s before any code here runs. That replaces the
        old `parseInt(id, 10)` + `isNaN` dance.

        Passing `instance=` makes the same serializer *update* instead of
        insert. No `partial=True`, so this is a true replace: a field the client
        omits is reset to its declared default, matching PUT semantics.
        """
        product = get_product(product_id)
        serializer = ProductSerializer(product, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

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

        product.delete()
        return Response({"success": True})
