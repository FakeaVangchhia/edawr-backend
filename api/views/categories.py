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

from django.db import transaction
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api import audit
from api.models import AuditLog, Category, Product
from api.paging import read_page
from api.permissions import AdminAPIView
from api.serializers import CategorySerializer, SuccessSerializer
from api.views.uploads import delete_stored_image


def get_category(category_id: int) -> Category:
    category = Category.objects.filter(pk=category_id).first()
    if category is None:
        raise NotFound("Category not found.")
    return category


class CategoryListCreateView(AdminAPIView):
    @extend_schema(responses=CategorySerializer(many=True))
    def get(self, request):
        """GET /api/categories?q=&status=&limit=&offset=

        Paged like products, with the total in `X-Total-Count`. A category list
        is short in practice, but "short today" is not a limit — the same
        reasoning that put a ceiling on the storefront's product page.
        """
        categories = Category.objects.order_by("id")

        query = (request.query_params.get("q") or "").strip()
        if query:
            categories = categories.filter(
                Q(name__icontains=query) | Q(description__icontains=query)
            )

        state = (request.query_params.get("status") or "").strip().lower()
        if state:
            categories = categories.filter(status=state)

        total = categories.count()
        limit, offset = read_page(request, default=100, maximum=200)
        page = categories[offset : offset + limit]

        response = Response(CategorySerializer(page, many=True).data)
        response["X-Total-Count"] = str(total)
        return response

    @extend_schema(request=CategorySerializer, responses={201: CategorySerializer})
    def post(self, request):
        serializer = CategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        audit.record(
            request, AuditLog.CREATE, "category", category.pk,
            f"Created category {category.name}",
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CategoryDetailView(AdminAPIView):
    @extend_schema(request=CategorySerializer, responses=CategorySerializer)
    def put(self, request, category_id: int):
        """PUT /api/categories/{category_id}

        **A rename has to carry its products with it.** `Product.category` is a
        free-text label, not a foreign key — that is how the schema arrived and
        the storefront joins the two by name to pick up `Category.image_url`. So
        renaming "Dairy & Bread" to "Dairy" leaves every product in the old
        category still saying "Dairy & Bread", which no category row matches any
        more: the rail loses its image, the category filter returns nothing, and
        thirty products silently fall out of the only navigation the storefront
        has. Nothing errors. It just quietly stops working.

        Both writes therefore land in one transaction. The proper fix is a real
        foreign key, which is a data migration across every product and order
        item and is not this change.
        """
        category = get_category(category_id)
        old_name = category.name

        with transaction.atomic():
            serializer = CategorySerializer(category, data=request.data)
            serializer.is_valid(raise_exception=True)
            before = {"name": old_name, "status": category.status,
                      "sort_order": category.sort_order}
            category = serializer.save()

            moved = 0
            if category.name != old_name:
                moved = Product.objects.filter(category=old_name).update(
                    category=category.name
                )

            summary = f"Updated category {category.name}"
            if moved:
                summary = f"Renamed {old_name} to {category.name} ({moved} products)"
            audit.record(
                request, AuditLog.UPDATE, "category", category.pk, summary,
                audit.diff(before, {"name": category.name, "status": category.status,
                                    "sort_order": category.sort_order}),
            )
        return Response(serializer.data)

    @extend_schema(responses=SuccessSerializer)
    def delete(self, request, category_id: int):
        """Children are re-parented to NULL, not deleted — the model declares
        `on_delete=models.SET_NULL` on the self-referencing `parent` field.
        """
        category = get_category(category_id)
        name = category.name
        image_url = category.image_url
        category.delete()
        # The tile image goes with it. Nothing else references it, and an upload
        # nothing points at is a file that will be on the disk forever.
        delete_stored_image(image_url)
        audit.record(
            request, AuditLog.DELETE, "category", category_id,
            f"Deleted category {name}",
        )
        return Response({"success": True})
