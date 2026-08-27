"""The operational settings a manager changes during a shift.

Everything else about this store's economics — fees, thresholds, the two
delivery tiers — is configured through environment variables, and that is right
for it: those are pricing decisions, they change rarely, and changing one should
require the same care as a deploy.

The handful of values here are a different kind of thing. They are operational
rather than commercial, they change *within* a shift, and the person who needs
to change them is standing behind the counter when they do. Requiring a redeploy
to pause checkout during a power cut is requiring the shop to keep promising
15-minute delivery it cannot make.

That is the whole reason `StoreSettings` is a table and not four more env vars.
The console's /settings screen was honest-but-read-only precisely because there
was nothing to write to.

**Either console role may change these.** They are how a Manager runs the store,
which is the line `api/permissions.py` draws: an Admin adds authority over *who*
runs it (`/api/admins`, `/api/audit`), not over the shop's opening hours.
"""

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from api import audit
from api.models import AuditLog, StoreSettings
from api.permissions import AdminAPIView
from api.serializers import StoreSettingsSerializer


class StoreSettingsView(AdminAPIView):
    """GET / PATCH /api/settings — the store's operational state."""

    @extend_schema(responses=StoreSettingsSerializer)
    def get(self, request):
        return Response(StoreSettingsSerializer(StoreSettings.load()).data)

    @extend_schema(request=StoreSettingsSerializer, responses=StoreSettingsSerializer)
    def patch(self, request):
        """Change only what was sent.

        PATCH rather than PUT, and there is no PUT here at all. A full replace
        would mean the pause switch had to be resent on every edit to the
        opening hours, and a client that forgot would silently reopen a store
        somebody had deliberately shut. `update_fields` names only the columns
        the caller actually sent.
        """
        row = StoreSettings.load()
        before = _snapshot(row)

        serializer = StoreSettingsSerializer(row, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        touched = list(serializer.validated_data.keys())
        if not touched:
            return Response(StoreSettingsSerializer(row).data)

        for field, value in serializer.validated_data.items():
            setattr(row, field, value)
        row.updated_at = timezone.now()
        row.save(update_fields=[*touched, "updated_at"])

        changes = audit.diff(before, _snapshot(row))
        if changes:
            audit.record(
                request,
                AuditLog.UPDATE,
                "settings",
                row.pk,
                _summarise(changes, row),
                changes,
            )
        return Response(StoreSettingsSerializer(row).data)


def _snapshot(row: StoreSettings) -> dict:
    return {
        "is_accepting_orders": row.is_accepting_orders,
        "closed_message": row.closed_message,
        "opens_at": str(row.opens_at),
        "closes_at": str(row.closes_at),
        "delivery_radius_km": row.delivery_radius_km,
        "store_latitude": row.store_latitude,
        "store_longitude": row.store_longitude,
    }


def _summarise(changes: dict, row: StoreSettings) -> str:
    """A sentence that reads usefully in the audit log.

    "Updated settings (is_accepting_orders)" is technically accurate and tells a
    reader nothing. Pausing the shop is the entry someone will actually go
    looking for, so it gets said in words.

    The new state is read off the row rather than out of `changes`, because
    `audit.diff` stringifies its values for the JSONField — so the "after" entry
    for a boolean is the string `"False"`, which is perfectly truthy and would
    log every pause as a resume.
    """
    if "is_accepting_orders" in changes:
        state = "Resumed" if row.is_accepting_orders else "Paused"
        return f"{state} new orders"
    return "Updated store settings (" + ", ".join(sorted(changes)) + ")"
