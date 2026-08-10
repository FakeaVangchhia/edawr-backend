"""Store staff (managers and delivery riders).

Both validation rules the FastAPI view performed by hand now live in
`UserSerializer`:

  - the role check, as `validate_role()`
  - the duplicate-phone check, as the `UniqueValidator` that ModelSerializer
    derives from `unique=True` on the model field

Both still return 400 with the same message. The second is the better trade:
the old code ran an extra SELECT to avoid an IntegrityError becoming a 500, and
DRF does that for you the moment the model says the column is unique.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response

from api.models import User
from api.permissions import AdminAPIView
from api.serializers import UserSerializer


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
