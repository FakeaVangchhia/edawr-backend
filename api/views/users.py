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

from api.models import Order, User
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
        users = User.objects.order_by("id")
        return Response(UserSerializer(users, many=True).data)

    @extend_schema(request=UserSerializer, responses={201: UserSerializer})
    def post(self, request):
        serializer = UserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
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
        serializer = UserSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
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
            return Response(
                {
                    "success": True,
                    "detail": (
                        "This rider has delivery history, so they were deactivated "
                        "rather than deleted. They can no longer sign in."
                    ),
                }
            )

        user.delete()
        return Response({"success": True})
