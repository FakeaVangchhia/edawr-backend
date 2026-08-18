"""The activity trail — Admin-only, read-only.

Admin-only because the log's job is to hold the people who run the store to
account, and a record its subjects can read selectively is worth less than one
they cannot. Read-only because nothing should ever be able to write a row
through the API: `api/audit.py::record` is the only writer, and it is called
from inside the operations it describes.

There is deliberately no delete endpoint. If this table ever needs trimming that
is a scheduled job with a retention policy, not a button next to the entries.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from api.models import AuditLog
from api.paging import read_page
from api.permissions import OwnerAdminAPIView
from api.serializers import AuditLogSerializer
from api.views.analytics import read_date


class AuditLogListView(OwnerAdminAPIView):
    """GET /api/audit?actor=&entity=&action=&q=&from=&to=&limit=&offset="""

    @extend_schema(responses=AuditLogSerializer(many=True))
    def get(self, request):
        rows = AuditLog.objects.all()  # Meta.ordering is newest-first.

        actor = (request.query_params.get("actor") or "").strip()
        if actor:
            # Numeric means "this account"; anything else is a name search, so
            # one field serves both "what did admin #3 do" and "what did Lal do".
            if actor.isdigit():
                rows = rows.filter(actor_id=int(actor))
            else:
                rows = rows.filter(actor_label__icontains=actor)

        for name in ("entity", "action", "actor_kind"):
            value = (request.query_params.get(name) or "").strip().lower()
            if value:
                rows = rows.filter(**{name: value})

        entity_id = (request.query_params.get("entity_id") or "").strip()
        if entity_id.isdigit():
            rows = rows.filter(entity_id=int(entity_id))

        query = (request.query_params.get("q") or "").strip()
        if query:
            rows = rows.filter(
                Q(summary__icontains=query) | Q(actor_label__icontains=query)
            )

        tz = ZoneInfo(settings.STORE_TIMEZONE)
        from_date = read_date(request, "from")
        if from_date:
            rows = rows.filter(
                created_at__gte=datetime.combine(from_date, time.min, tzinfo=tz)
            )
        to_date = read_date(request, "to")
        if to_date:
            # Half-open against midnight after `to_date`, matching analytics.py,
            # so an inclusive date range does not silently drop the last day.
            rows = rows.filter(
                created_at__lt=datetime.combine(
                    to_date + timedelta(days=1), time.min, tzinfo=tz
                )
            )

        total = rows.count()
        limit, offset = read_page(request, default=50, maximum=200)
        data = AuditLogSerializer(rows[offset : offset + limit], many=True).data
        response = Response(data)
        response["X-Total-Count"] = str(total)
        return response
