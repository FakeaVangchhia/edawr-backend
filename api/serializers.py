"""Serializers — the direct replacement for `app/schemas.py` (Pydantic).

Same two jobs as before:

1. **Validate what comes in.** `serializer.is_valid(raise_exception=True)` is
   the explicit version of FastAPI's automatic body validation. A bad body
   raises `ValidationError` -> HTTP 400 and the view body never runs.
2. **Control what goes out.** `fields = [...]` filters the output exactly the
   way `response_model=` did — an attribute not listed here cannot leak, which
   is why `AdminUser.password_hash` never appears in a response.

The one genuinely new idea is that **a DRF serializer also writes**. Pydantic
validated a dict and you constructed the ORM object yourself; a
`ModelSerializer` knows its model, so `serializer.save()` does the insert or
update for you. That is why there is one `ProductSerializer` here where
schemas.py needed `ProductCreate`, `ProductUpdate` *and* `ProductOut`.

Two details that keep behaviour identical to the Pydantic version:

- **`allow_blank=True` on optional text fields.** DRF rejects `""` by default;
  Pydantic accepted it. The product editor submits `""` for every optional text
  input it leaves empty, so without this, saving a product with no SKU would
  start failing with a 400.
- **Explicit `default=` on optional fields.** DRF applies a field default during
  a full (non-partial) update, so `PUT` still *replaces* the resource the way
  the Pydantic `ProductUpdate` model did, instead of quietly leaving omitted
  fields at their old values.
"""

from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from api.models import Category, Order, OrderItem, Product, User
from api.security import hash_password

# Reused by every optional free-text field. `default=None` (rather than simply
# `required=False`) is what preserves PUT-replaces-everything semantics.
OPTIONAL_TEXT = {"required": False, "allow_null": True, "allow_blank": True, "default": None}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class LoginSerializer(serializers.Serializer):
    """Input only — a plain `Serializer`, because there is no model behind it.

    This is the Pydantic-equivalent tier of DRF: declare fields, call
    `is_valid()`, read `validated_data`. Reach for `ModelSerializer` only when a
    model is actually involved.
    """

    email = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class LoginResponseSerializer(serializers.Serializer):
    """Output only. Declared so drf-spectacular can document the response."""

    access_token = serializers.CharField()
    token_type = serializers.CharField(default="bearer")
    username = serializers.CharField()


class RiderLoginSerializer(serializers.Serializer):
    """Rider credentials: the phone number on their staff record, plus a PIN.

    `trim_whitespace=False` on the PIN for the same reason as on a password —
    stripping input silently turns a wrong credential into a different wrong
    credential, and would let " 1234 " authenticate as "1234".
    """

    phone = serializers.CharField()
    pin = serializers.CharField(trim_whitespace=False)


class RiderLoginResponseSerializer(serializers.Serializer):
    access_token = serializers.CharField()
    token_type = serializers.CharField(default="bearer")
    rider = serializers.DictField()


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = [
            "id", "name", "sku", "barcode", "category", "brand", "unit",
            "price", "cost_price", "mrp", "stock", "reorder_level", "status",
            "location", "supplier_name", "supplier_phone", "description",
            "image_url", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        # `extra_kwargs` tweaks the fields ModelSerializer generated from the
        # model, without having to redeclare them by hand.
        extra_kwargs = {
            "name": {"required": True, "allow_blank": False},
            "sku": OPTIONAL_TEXT,
            "barcode": OPTIONAL_TEXT,
            "brand": OPTIONAL_TEXT,
            "location": OPTIONAL_TEXT,
            "supplier_name": OPTIONAL_TEXT,
            "supplier_phone": OPTIONAL_TEXT,
            "description": OPTIONAL_TEXT,
            "image_url": OPTIONAL_TEXT,
            "category": {**OPTIONAL_TEXT, "default": "General"},
            "unit": {**OPTIONAL_TEXT, "default": "unit"},
            "price": {"required": False, "default": 0.0},
            "cost_price": {"required": False, "default": 0.0},
            "mrp": {"required": False, "default": 0.0},
            "stock": {"required": False, "default": 0},
            "reorder_level": {"required": False, "default": 0},
            "status": {"required": False, "default": "active"},
        }


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
class CategorySerializer(serializers.ModelSerializer):
    # The model field is `parent` (a Category object); the API has always spoken
    # `parent_id` (an integer). `source="parent"` bridges the two, and
    # `PrimaryKeyRelatedField` checks the row exists — the manual
    # `db.get(Category, parent_id)` lookup the FastAPI view had to do.
    parent_id = serializers.PrimaryKeyRelatedField(
        source="parent",
        queryset=Category.objects.all(),
        required=False,
        allow_null=True,
        default=None,
        error_messages={"does_not_exist": "Parent category not found."},
    )

    class Meta:
        model = Category
        fields = ["id", "name", "description", "parent_id", "status", "created_at"]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {
            "name": {"required": True, "allow_blank": False},
            "description": OPTIONAL_TEXT,
            "status": {"required": False, "default": "active"},
        }

    def validate(self, attrs):
        """Object-level validation — runs after every field passed on its own.

        Use `validate()` (not `validate_<field>()`) when a rule needs more than
        one value. Here it needs the field *and* the instance being updated,
        which is only available on the serializer.
        """
        parent = attrs.get("parent")
        if parent is not None and self.instance is not None and parent.pk == self.instance.pk:
            raise serializers.ValidationError("A category cannot be its own parent.")
        return attrs


# --------------------------------------------------------------------------
# Users (store staff / riders)
# --------------------------------------------------------------------------
class UserSerializer(serializers.ModelSerializer):
    # Overrides the ChoiceField that `choices=` on the model would generate, so
    # the error message stays the one the old API returned.
    role = serializers.CharField()

    # The model marks `phone` unique, so ModelSerializer would attach a
    # UniqueValidator by itself — the pre-insert existence check the FastAPI view
    # did by hand, to turn an IntegrityError 500 into a 400. It is declared here
    # only to control the wording.
    phone = serializers.CharField(
        validators=[
            UniqueValidator(
                queryset=User.objects.all(),
                message="A user with that phone already exists.",
            )
        ]
    )

    # `write_only` is the whole point: a manager can set a rider's PIN through
    # this serializer, but no response can ever echo it back, and `pin_hash`
    # is absent from `fields` so the stored hash cannot leak either.
    pin = serializers.CharField(
        write_only=True,
        required=False,
        allow_null=True,
        min_length=4,
        trim_whitespace=False,
        help_text="Rider sign-in PIN. Omit to leave the rider unable to sign in.",
    )

    class Meta:
        model = User
        fields = [
            "id", "name", "role", "phone", "pin", "base_latitude",
            "base_longitude", "service_radius_km", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {
            "name": {"required": True, "allow_blank": False},
            "base_latitude": {"required": False, "default": 23.7272},
            "base_longitude": {"required": False, "default": 92.7178},
            "service_radius_km": {"required": False, "default": 10.0},
        }

    def create(self, validated_data):
        """Hash the PIN on the way in; never store what the client sent."""
        pin = validated_data.pop("pin", None)
        user = super().create(validated_data)
        if pin:
            user.pin_hash = hash_password(pin)
            user.save(update_fields=["pin_hash"])
        return user

    def validate_role(self, value: str) -> str:
        """`validate_<field_name>` runs for one field and returns its value.

        Raising here produces a 400. The exception handler prefixes the field
        name, so the message must not repeat it — the client sees
        {"detail": "role: Must be one of ['delivery', 'manager']."}.
        """
        valid = sorted(choice for choice, _ in User.ROLE_CHOICES)
        if value not in valid:
            raise serializers.ValidationError(f"Must be one of {valid}.")
        return value


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
class OrderItemSerializer(serializers.ModelSerializer):
    # Django gives every FK a free `<name>_id` integer attribute, so these read
    # straight off the row without touching the related table.
    order_id = serializers.IntegerField(read_only=True)
    product_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "order_id", "product_id", "quantity", "name", "price"]


class OrderSerializer(serializers.ModelSerializer):
    # `many=True` + `read_only=True` nests the whole list. This is the
    # equivalent of `items: list[OrderItemOut]` on the Pydantic OrderOut, and it
    # is why the views call `.prefetch_related("items")` — without it, DRF would
    # issue one extra query per order.
    items = OrderItemSerializer(many=True, read_only=True)
    delivery_boy_id = serializers.IntegerField(read_only=True)
    offered_to_delivery_boy_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id", "customer_phone", "customer_name", "customer_address",
            "customer_latitude", "customer_longitude", "status",
            "delivery_boy_id", "offered_to_delivery_boy_id",
            "offered_distance_km", "created_at", "items",
        ]


class AssignSerializer(serializers.Serializer):
    delivery_boy_id = serializers.IntegerField()


class StatusSerializer(serializers.Serializer):
    status = serializers.CharField()

    def validate_status(self, value: str) -> str:
        valid = sorted(choice for choice, _ in Order.STATUS_CHOICES)
        if value not in valid:
            raise serializers.ValidationError(f"Must be one of {valid}.")
        return value


# --------------------------------------------------------------------------
# Delivery dashboard
# --------------------------------------------------------------------------
class DeliveryDashboardSerializer(serializers.Serializer):
    """A composite response with no model of its own.

    `OrderSerializer` is reused three times here. Serializers nest freely — the
    same composition Pydantic models gave you.
    """

    incoming_orders = OrderSerializer(many=True)
    active_order = OrderSerializer(allow_null=True)
    recent_orders = OrderSerializer(many=True)


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------
class UploadResponseSerializer(serializers.Serializer):
    image_url = serializers.CharField()


class SuccessSerializer(serializers.Serializer):
    """`{"success": true}` — what the delete and reject endpoints return."""

    success = serializers.BooleanField(default=True)
