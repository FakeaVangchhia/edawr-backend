"""Public storefront catalog.

This is the only *public* read of product data, and it is a separate module from
`products.py` for exactly that reason: `products.py` inherits `AdminAPIView`,
and this must not. Splitting by access level rather than by table keeps the
guard honest — you cannot accidentally expose an admin endpoint by adding a
method to the wrong class.

It also returns a narrower row set (active products only), which is why the
frontend has two endpoints for one table in the first place.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import Product
from api.serializers import ProductSerializer


class StoreProductListView(APIView):
    """No `permission_classes` — inherits the project-wide `AllowAny`."""

    @extend_schema(responses=ProductSerializer(many=True))
    def get(self, request):
        """GET /api/store/products — active products only.

        `status__iexact="active"` is a Django *field lookup*: the double
        underscore separates the field from the operator. It compiles to a
        case-insensitive SQL comparison, which is what the old
        `func.lower(Product.status) == "active"` did, and it matters because
        rows carried over from Supabase use both "active" and "Active".
        """
        products = Product.objects.filter(status__iexact="active").order_by("id")
        return Response(ProductSerializer(products, many=True).data)
