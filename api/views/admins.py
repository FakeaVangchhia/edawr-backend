"""Console accounts — the Admin-only surface.

This is what makes the two roles real. A Manager runs the store; an Admin
additionally decides who runs the store, which is this file plus `audit.py`.

**Three guards, and all three exist to prevent the same accident.** Console
accounts are the only records that can revoke access to the screen that manages
console accounts, so a careless edit here is not "a mistake you fix in the UI" —
it is a database nobody can administer without shell access to production.

    1. You cannot change your own role.
    2. You cannot deactivate yourself.
    3. You cannot demote or deactivate the last active Admin.

The first two are about the click you did not mean to make. The third is about
the one you did: demoting the only Admin is a perfectly coherent request that
leaves the store permanently unadministrable, so it has to be refused even
though the user meant it.

Each returns **409**, not 400. The request body is fine; it is the *state* of the
account table that makes the operation impossible — the same distinction
`Order.advance_status` draws between an illegal transition and a malformed one.
"""

from django.db import transaction
from django.db.models import F
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api import audit
from api.models import AdminUser, AuditLog
from api.paging import read_page
from api.permissions import OwnerAdminAPIView
from api.serializers import AdminUserSerializer, SuccessSerializer


def get_admin(admin_id: int) -> AdminUser:
    account = AdminUser.objects.filter(pk=admin_id).first()
    if account is None:
        raise NotFound("Account not found.")
    return account


def conflict(message: str) -> Response:
    return Response({"detail": message}, status=status.HTTP_409_CONFLICT)


def other_active_admins(exclude_pk: int):
    """Active Admin-role accounts that are *not* this one."""
    return AdminUser.objects.filter(
        role=AdminUser.ADMIN, is_active=True
    ).exclude(pk=exclude_pk)


class AdminListCreateView(OwnerAdminAPIView):
    """GET/POST /api/admins"""

    @extend_schema(responses=AdminUserSerializer(many=True))
    def get(self, request):
        rows = AdminUser.objects.order_by("id")

        query = (request.query_params.get("q") or "").strip()
        if query:
            from django.db.models import Q

            rows = rows.filter(Q(email__icontains=query) | Q(name__icontains=query))

        role = (request.query_params.get("role") or "").strip().lower()
        if role:
            rows = rows.filter(role=role)

        total = rows.count()
        limit, offset = read_page(request, default=50, maximum=200)
        data = AdminUserSerializer(rows[offset : offset + limit], many=True).data
        response = Response(data)
        response["X-Total-Count"] = str(total)
        return response

    @extend_schema(request=AdminUserSerializer, responses=AdminUserSerializer)
    def post(self, request):
        serializer = AdminUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.save()
        audit.record(
            request, AuditLog.CREATE, "admin", account.pk,
            f"Created {account.get_role_display()} account {account.email}",
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AdminDetailView(OwnerAdminAPIView):
    """PUT/DELETE /api/admins/<id>"""

    @extend_schema(request=AdminUserSerializer, responses=AdminUserSerializer)
    def put(self, request, admin_id: int):
        account = get_admin(admin_id)
        # Lock the row for the duration: guard 3 reads the rest of the table and
        # then writes this one, and two concurrent demotions that each see the
        # other as "the last Admin" would otherwise both be allowed through.
        with transaction.atomic():
            account = AdminUser.objects.select_for_update().get(pk=account.pk)

            # `partial=True` deliberately, matching UserDetailView: an omitted
            # `password` must leave the stored hash alone rather than clear it.
            serializer = AdminUserSerializer(account, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)

            new_role = serializer.validated_data.get("role", account.role)
            new_active = serializer.validated_data.get("is_active", account.is_active)
            is_self = account.pk == request.user.pk

            if is_self and new_role != account.role:
                return conflict(
                    "You cannot change your own role. Ask another Admin to do it."
                )
            if is_self and not new_active:
                return conflict("You cannot deactivate your own account.")

            losing_admin = account.role == AdminUser.ADMIN and (
                new_role != AdminUser.ADMIN or not new_active
            )
            if losing_admin and not other_active_admins(account.pk).exists():
                return conflict(
                    "This is the last active Admin. Promote another account to "
                    "Admin first, or the console could no longer be administered."
                )

            before = {
                "email": account.email, "name": account.name,
                "role": account.role, "is_active": account.is_active,
            }
            account = serializer.save()
            changes = audit.diff(before, {
                "email": account.email, "name": account.name,
                "role": account.role, "is_active": account.is_active,
            })
            # `password_reset`, not `password`: see the same note in users.py.
            # `record()` drops credential-named keys, marker or not.
            if request.data.get("password"):
                changes["password_reset"] = ["no", "yes"]
                # A reset that leaves the old sessions working is not a reset.
                # The usual reason to change someone's password is that the old
                # one is compromised, and whoever compromised it is holding a
                # token that outlives the change by up to twelve hours.
                AdminUser.objects.filter(pk=account.pk).update(
                    token_version=F("token_version") + 1
                )

            audit.record(
                request, AuditLog.UPDATE, "admin", account.pk,
                f"Updated account {account.email}", changes,
            )
        return Response(serializer.data)

    @extend_schema(responses=SuccessSerializer)
    def delete(self, request, admin_id: int):
        account = get_admin(admin_id)
        with transaction.atomic():
            account = AdminUser.objects.select_for_update().get(pk=account.pk)

            if account.pk == request.user.pk:
                return conflict("You cannot delete your own account.")
            if account.role == AdminUser.ADMIN and not other_active_admins(account.pk).exists():
                return conflict(
                    "This is the last active Admin. Promote another account to "
                    "Admin first."
                )

            email = account.email
            # Deactivate rather than delete. The audit log names accounts by a
            # denormalised label so it survives either, but keeping the row means
            # a re-hired manager keeps one identity instead of accumulating two,
            # and `is_active` is re-checked on every request so this revokes
            # access immediately.
            account.is_active = False
            account.save(update_fields=["is_active"])
            audit.record(
                request, AuditLog.DELETE, "admin", account.pk,
                f"Deactivated account {email}",
            )
        return Response(
            {"success": True, "detail": f"{email} can no longer sign in."}
        )
