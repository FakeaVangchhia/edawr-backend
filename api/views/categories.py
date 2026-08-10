"""Admin category CRUD.

Structurally identical to products.py. The one thing worth reading for is where
the validation went: the FastAPI version checked "does this parent exist?" and
"is this category its own parent?" inside the route body. Both moved into
`CategorySerializer` — the first as a `PrimaryKeyRelatedField` queryset lookup,
the second as `validate()`.

That is the DRF habit: rules about the *shape and consistency of the data* go in
the serializer; rules about *what this request is allowed to do* stay in the
view.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api.models import Category
from api.permissions import AdminAPIView
from api.serializers import CategorySerializer, SuccessSerializer


def get_category(category_id: int) -> Category:
    category = Category.objects.filter(pk=category_id).first()
    if category is None:
        raise NotFound("Category not found.")
    return category


class CategoryListCreateView(AdminAPIView):
    @extend_schema(responses=CategorySerializer(many=True))
    def get(self, request):
        categories = Category.objects.order_by("id")
        return Response(CategorySerializer(categories, many=True).data)

    @extend_schema(request=CategorySerializer, responses={201: CategorySerializer})
    def post(self, request):
        serializer = CategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CategoryDetailView(AdminAPIView):
    @extend_schema(request=CategorySerializer, responses=CategorySerializer)
    def put(self, request, category_id: int):
        category = get_category(category_id)
        serializer = CategorySerializer(category, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(responses=SuccessSerializer)
    def delete(self, request, category_id: int):
        """Children are re-parented to NULL, not deleted — the model declares
        `on_delete=models.SET_NULL` on the self-referencing `parent` field.
        """
        category = get_category(category_id)
        category.delete()
        return Response({"success": True})
