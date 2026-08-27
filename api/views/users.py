"""Store staff (managers and delivery riders).

Both validation rules the FastAPI view performed by hand live in
`UserSerializer`: the role check as `validate_role()`, and the duplicate-phone
check as the `UniqueValidator` that ModelSerializer derives from `unique=True`
on the model field.

`PUT` exists here for one reason above the others: **a forgotten PIN used to
need a shell**. There was no way to rotate a rider's credential through the API
at all, which meant the realistic recovery was for someone to reuse a PIN they
could remember, on every rider.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from django.db.models import F, Q

from api import audit
from api.models import AuditLog, Order, User
from api.paging import read_page
from api.permissions import AdminAPIView
from api.serializers import SuccessSerializer, UserSerializer


def get_user(user_id: int) -> User:
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise NotFound("User not found.")
    return user


class UserListCreateView(AdminAPIView):
    @extend_schema(responses=UserSerializer(many=True))
    def get(self, request):
        """GET /api/users?role=&q=&active=&limit=&offset=

        `?role=delivery` is what the console's rider pickers want; the staff
        screen wants the unfiltered list. Note this endpoint returns *both*
        roles, unlike `/api/delivery/riders`, which is riders-only because the
        rider app has no business knowing the managers.
        """
        users = User.objects.order_by("id")

        role = (request.query_params.get("role") or "").strip().lower()
        if role:
            users = users.filter(role=role)

        query = (request.query_params.get("q") or "").strip()
        if query:
            users = users.filter(Q(name__icontains=query) | Q(phone__icontains=query))

        active = (request.query_params.get("active") or "").strip().lower()
        if active in {"1", "true", "yes"}:
            users = users.filter(is_active=True)
        elif active in {"0", "false", "no"}:
            users = users.filter(is_active=False)

        total = users.count()
        limit, offset = read_page(request, default=100, maximum=200)
        users = users[offset : offset + limit]
        response = Response(UserSerializer(users, many=True).data)
        response["X-Total-Count"] = str(total)
        return response

    @extend_schema(request=UserSerializer, responses={201: UserSerializer})
    def post(self, request):
        serializer = UserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        audit.record(
            request, AuditLog.CREATE, "staff", user.pk,
            f"Added {user.get_role_display().lower()} {user.name} ({user.phone})",
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class UserDetailView(AdminAPIView):
    @extend_schema(request=UserSerializer, responses=UserSerializer)
    def put(self, request, user_id: int):
        """PUT /api/users/{user_id} — update a staff member, including their PIN.

        `partial=True` despite being a PUT. A strict PUT would reset every
        omitted field to its default, and the field most often omitted here is
        `pin` — which is write-only, so a client that reads a user and writes it
        back *cannot* send it. Under replace semantics that round trip would
        silently clear the rider's ability to sign in.
        """
        user = get_user(user_id)
        before = {"name": user.name, "role": user.role, "phone": user.phone,
                  "is_active": user.is_active, "is_available": user.is_available,
                  "service_radius_km": user.service_radius_km}
        serializer = UserSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        changes = audit.diff(before, {
            "name": user.name, "role": user.role, "phone": user.phone,
            "is_active": user.is_active, "is_available": user.is_available,
            "service_radius_km": user.service_radius_km,
        })
        # A PIN reset would otherwise leave no trace: `pin` is write-only, and
        # `audit.diff` redacts it by name. Note the key is `pin_reset`, not
        # `pin` — `record()` strips anything named like a credential, so a
        # marker called "pin" would be deleted along with the secret it stands
        # in for. Record that it happened; never what it was changed to.
        if request.data.get("pin"):
            changes["pin_reset"] = ["no", "yes"]
            # And retire the rider's current tokens with it — a PIN is reset
            # because the old one is no longer trusted, and a twelve-hour token
            # minted under it would otherwise outlive the decision.
            User.objects.filter(pk=user.pk).update(
                token_version=F("token_version") + 1
            )
        audit.record(
            request, AuditLog.UPDATE, "staff", user.pk,
            f"Updated {user.name}", changes,
        )
        return Response(serializer.data)

    @extend_schema(responses={200: SuccessSerializer, 409: SuccessSerializer})
    def delete(self, request, user_id: int):
        """DELETE /api/users/{user_id} — deactivate, or delete if never used.

        A rider who has delivered anything is deactivated rather than deleted:
        `Order.delivery_boy` is `SET_NULL`, so a real delete would quietly erase
        who delivered every past order. Deactivating revokes their access —
        `RiderJWTAuthentication` re-checks `is_active` on every request — while
        keeping the record intact.
        """
        user = get_user(user_id)

        if Order.objects.filter(delivery_boy_id=user.pk).exists():
            user.is_active = False
            user.is_available = False
            user.save(update_fields=["is_active", "is_available"])
            audit.record(
                request, AuditLog.DELETE, "staff", user.pk,
                f"Deactivated {user.name} (has delivery history)",
            )
            return Response(
                {
                    "success": True,
                    "detail": (
                        "This rider has delivery history, so they were deactivated "
                        "rather than deleted. They can no longer sign in."
                    ),
                }
            )

        name, phone = user.name, user.phone
        user.delete()
        audit.record(
            request, AuditLog.DELETE, "staff", user_id,
            f"Deleted {name} ({phone})",
        )
        return Response({"success": True})
