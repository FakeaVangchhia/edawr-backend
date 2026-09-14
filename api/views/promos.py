"""Admin banner CRUD.

Structurally identical to categories.py, minus the rename cascade: nothing
references a banner by name. Either console role may edit — a Manager runs the
store, and what goes at the top of the home page this week is running the
store — and every write lands in the audit log, because a banner is the most
visible thing a console user can change and "who put that up?" is a question
the owner will ask.

The public read is not here. `views/store.py::StorePromoListView` serves the
storefront through `Promo.live()` and `StorePromoSerializer`, so what a customer
sees and what a Manager edits are two views with two allow-lists.
"""

from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api import audit, storage
from api.models import AuditLog, Promo
from api.paging import read_choice, read_page
from api.permissions import AdminAPIView
from api.serializers import PromoSerializer, SuccessSerializer
from api.views.products import CATALOGUE_STATUSES

# The columns worth a before/after in the audit row. The dates are included
# because "why did the Diwali banner vanish on the 3rd?" is answered by them.
AUDITED = ("title", "link", "sort_order", "status", "starts_at", "ends_at")


def get_promo(promo_id: int) -> Promo:
    promo = Promo.objects.filter(pk=promo_id).first()
    if promo is None:
        raise NotFound("Promotion not found.")
    return promo


def snapshot(promo: Promo) -> dict:
    # `audit.diff` stringifies, so a datetime lands as its str() — readable
    # enough for a log that is read rather than parsed.
    return {field: getattr(promo, field) for field in AUDITED}


class PromoListCreateView(AdminAPIView):
    @extend_schema(responses=PromoSerializer(many=True))
    def get(self, request):
        """GET /api/promos?q=&status=&limit=&offset=

        Every banner, live or not, in rail order — the console needs to see
        the one that expired last week to bring it back. Paged like
        categories, with the total in `X-Total-Count`.
        """
        promos = Promo.objects.order_by("sort_order", "-created_at")

        query = (request.query_params.get("q") or "").strip()
        if query:
            promos = promos.filter(Q(title__icontains=query) | Q(subtitle__icontains=query))

        state = read_choice(request, "status", CATALOGUE_STATUSES)
        if state:
            promos = promos.filter(status=state)

        total = promos.count()
        limit, offset = read_page(request, default=100, maximum=200)
        page = promos[offset : offset + limit]

        response = Response(PromoSerializer(page, many=True).data)
        response["X-Total-Count"] = str(total)
        return response

    @extend_schema(request=PromoSerializer, responses={201: PromoSerializer})
    def post(self, request):
        serializer = PromoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        promo = serializer.save()
        audit.record(
            request, AuditLog.CREATE, "promo", promo.pk,
            f"Created promotion {promo.title}",
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class PromoDetailView(AdminAPIView):
    @extend_schema(request=PromoSerializer, responses=PromoSerializer)
    def put(self, request, promo_id: int):
        """PUT /api/promos/{promo_id} — a full replace, like every other PUT here."""
        promo = get_promo(promo_id)
        before = snapshot(promo)
        old_image = promo.image_url

        serializer = PromoSerializer(promo, data=request.data)
        serializer.is_valid(raise_exception=True)
        promo = serializer.save()

        # A replaced banner image is an orphan on the disk or in the bucket
        # the moment the row stops pointing at it.
        if old_image and old_image != promo.image_url:
            storage.delete(old_image)

        audit.record(
            request, AuditLog.UPDATE, "promo", promo.pk,
            f"Updated promotion {promo.title}",
            audit.diff(before, snapshot(promo)),
        )
        return Response(serializer.data)

    @extend_schema(responses=SuccessSerializer)
    def delete(self, request, promo_id: int):
        promo = get_promo(promo_id)
        title = promo.title
        image_url = promo.image_url
        promo.delete()
        storage.delete(image_url)
        audit.record(
            request, AuditLog.DELETE, "promo", promo_id,
            f"Deleted promotion {title}",
        )
        return Response({"success": True})
