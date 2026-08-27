"""Serializers — validate what comes in, control what goes out.

Two jobs, and the second one is the security-relevant one: `fields = [...]` is
an allow-list, so an attribute not named there cannot leak. That is why there
are *two* product serializers. `ProductSerializer` is the admin view and carries
`cost_price`, supplier details and shelf location; `StoreProductSerializer` is
what the public storefront returns and carries none of them. Splitting the
classes means exposing margin data to customers would require someone to
deliberately change the public serializer, rather than merely forgetting to
exclude a field they added to the model.

Two DRF details that keep behaviour predictable:

- **`allow_blank=True` on optional text fields.** DRF rejects `""` by default.
  The product editor submits `""` for every optional input it leaves empty, so
  without this, saving a product with no SKU fails with a 400.
- **Explicit `default=` on optional fields.** DRF applies a field default during
  a full (non-partial) update, so `PUT` genuinely *replaces* the resource
  instead of quietly leaving omitted fields at their old values.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from api.models import (
    AdminUser,
    AuditLog,
    Category,
    Customer,
    Order,
    OrderItem,
    Product,
    RiderDevice,
    StoreSettings,
    User,
)
from api.pricing import free_delivery_shortfall, money
from api.security import hash_password, validate_password_strength
from api.validators import PhoneField

# Reused by every optional free-text field. `default=None` (rather than simply
# `required=False`) is what preserves PUT-replaces-everything semantics.
OPTIONAL_TEXT = {"required": False, "allow_null": True, "allow_blank": True, "default": None}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class LoginSerializer(serializers.Serializer):
    """Input only — a plain `Serializer`, because there is no model behind it."""

    email = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class LoginResponseSerializer(serializers.Serializer):
    """Output only. Declared so drf-spectacular can document the response.

    `role` was added when the console gained two of them. The client needs it to
    decide which navigation to render — but note that decision is cosmetic:
    `IsOwnerAdmin` re-checks the role from the database on every request, so a
    client that lies to itself about this field gains nothing but a link that
    403s. `username` is kept as-is for the existing storefront console, which
    reads it and knows nothing about roles.
    """

    access_token = serializers.CharField()
    token_type = serializers.CharField(default="bearer")
    username = serializers.CharField()
    email = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    role = serializers.ChoiceField(choices=AdminUser.ROLE_CHOICES)


class RiderLoginSerializer(serializers.Serializer):
    """Rider credentials: the phone number on their staff record, plus a PIN.

    The phone goes through the same normalisation as the one stored on the
    staff record, so a rider who types their number without the country code
    still matches the row a manager created with it. Two spellings of one number
    would otherwise be two different accounts, only one of which can sign in.

    `trim_whitespace=False` on the PIN for the same reason as on a password —
    stripping input silently turns a wrong credential into a different wrong
    credential, and would let " 4813 " authenticate as "4813".
    """

    phone = PhoneField()
    pin = serializers.CharField(trim_whitespace=False)


# --------------------------------------------------------------------------
# Customer accounts
# --------------------------------------------------------------------------
# `max_length` on every password field is a cost control, not a policy: PBKDF2
# runs over whatever arrives, DRF caps nothing by default, and a one-megabyte
# password is free CPU for whoever sends it.
PASSWORD_INPUT = {"trim_whitespace": False, "max_length": 128, "write_only": True}


class CustomerSerializer(serializers.ModelSerializer):
    """A customer account, as the account's own owner sees it.

    **`fields` is an explicit list rather than an `exclude`.** `password_hash`
    and `token_version` are on this model, and a serializer that names what it
    omits leaks a new column the moment someone adds one. The same reasoning
    splits `StoreProductSerializer` from `ProductSerializer`.

    `phone_verified` is a boolean derived from the timestamp rather than the
    timestamp itself. The client only ever branches on it, and exposing a date
    invites a screen that says "verified on 3 March" — a fact this store cannot
    currently establish at all.
    """

    phone_verified = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = ["id", "phone", "name", "phone_verified", "created_at"]

    def get_phone_verified(self, obj: Customer) -> bool:
        return obj.phone_verified_at is not None


class CustomerSignupSerializer(serializers.Serializer):
    """What it takes to create an account: a number and a password.

    No email, because there is nowhere to send one, and a field the store
    cannot act on is a field that misleads whoever fills it in.

    `claim_token` is optional and is the tracking token of an order the caller
    is holding — the one they just placed, when someone signs up from checkout.
    It links that order to the new account. **The token is the evidence, not
    the phone number**: possession of it is already what authorises the public
    tracking endpoint to show a name and an address, so accepting it here grants
    nothing that was not already granted. The phone number, by contrast, proves
    nothing until an OTP says otherwise — which is why it links exactly the one
    order named, and not every order that shares a number.
    """

    phone = PhoneField()
    password = serializers.CharField(**PASSWORD_INPUT)
    name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    claim_token = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        # Validated here rather than in `validate_password` so the phone and
        # name are already parsed and can be handed to the similarity check —
        # which is what refuses "9812345678" as the password for +919812345678,
        # the single likeliest weak password on an account keyed by phone.
        validate_password_strength(
            attrs["password"],
            user=Customer(phone=attrs["phone"], name=attrs.get("name", "")),
        )
        return attrs


class CustomerLoginSerializer(serializers.Serializer):
    """Credentials only.

    **No length or strength rule here, deliberately.** Checking a password is
    not setting one: a rule tightened later must not lock out an account whose
    password predates it, and rejecting a short attempt before looking anything
    up is a free hint about the policy.
    """

    phone = PhoneField()
    password = serializers.CharField(trim_whitespace=False)


class CustomerProfileSerializer(serializers.Serializer):
    """The one thing a customer may change about themselves.

    Not the phone: it is the account's identity, so changing it is closer to
    creating a different account, and doing it silently would move whatever
    `phone_verified_at` claimed about the old number onto a new one.
    """

    name = serializers.CharField(max_length=120, allow_blank=True)


class CustomerPasswordSerializer(serializers.Serializer):
    """Change a password, proving you know the current one.

    Requiring `current_password` is what stops a borrowed phone with an open
    session from becoming a permanent takeover: the session alone is enough to
    place an order, and deliberately not enough to lock the owner out of their
    own account.
    """

    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(**PASSWORD_INPUT)

    def validate_new_password(self, value: str) -> str:
        validate_password_strength(value, user=self.context.get("customer"))
        return value


class CustomerClaimSerializer(serializers.Serializer):
    """The tracking token of an order the caller is holding."""

    tracking_token = serializers.CharField(max_length=64)


class CustomerTokenResponseSerializer(serializers.Serializer):
    """Output only, for drf-spectacular. Mirrors RiderLoginResponseSerializer."""

    access_token = serializers.CharField()
    token_type = serializers.CharField(default="bearer")
    customer = CustomerSerializer()


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
class ProductSerializer(serializers.ModelSerializer):
    """The admin view of a product. Includes margin and supplier data."""

    discount_percent = serializers.IntegerField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "name", "sku", "barcode", "category", "brand", "unit",
            "price", "cost_price", "mrp", "stock", "reorder_level", "status",
            "location", "supplier_name", "supplier_phone", "description",
            "image_url", "discount_percent", "created_at",
        ]
        read_only_fields = ["id", "created_at", "discount_percent"]
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
            "price": {"required": False, "default": Decimal("0.00")},
            "cost_price": {"required": False, "default": Decimal("0.00")},
            "mrp": {"required": False, "default": Decimal("0.00")},
            "stock": {"required": False, "default": 0},
            "reorder_level": {"required": False, "default": 0},
            "status": {"required": False, "default": Product.ACTIVE},
        }

    def validate(self, attrs):
        """Reject an MRP below the selling price.

        The storefront renders `mrp` struck through next to `price`. If MRP were
        the lower number the badge would advertise a negative discount, which
        looks like a bug to a customer and like a pricing error to a regulator.
        """
        price = attrs.get("price", getattr(self.instance, "price", Decimal("0.00")))
        mrp = attrs.get("mrp", getattr(self.instance, "mrp", Decimal("0.00")))
        if mrp and price and mrp < price:
            raise serializers.ValidationError(
                {"mrp": "MRP cannot be lower than the selling price."}
            )
        return attrs


class StoreProductSerializer(serializers.ModelSerializer):
    """The public view of a product.

    Note what is absent: `cost_price`, `supplier_name`, `supplier_phone`,
    `location`, `reorder_level`. Those are the store's business, not the
    customer's, and `stock` is reduced to a boolean plus a low-stock hint so a
    competitor cannot read exact inventory levels off the storefront.
    """

    in_stock = serializers.BooleanField(source="is_in_stock", read_only=True)
    discount_percent = serializers.IntegerField(read_only=True)
    low_stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "name", "category", "brand", "unit", "price", "mrp",
            "description", "image_url", "in_stock", "low_stock",
            "discount_percent",
        ]
        read_only_fields = fields

    def get_low_stock(self, product: Product) -> bool:
        """Drives the "Only a few left" nudge without publishing a number."""
        return 0 < product.stock <= 5


class StoreCategorySerializer(serializers.Serializer):
    """A tile in the storefront's category rail.

    Built from a dict assembled in the view rather than straight off the model,
    because `product_count` comes from a separate aggregate over Product — the
    two tables are joined by category *name*, which is how the schema has always
    related them.
    """

    name = serializers.CharField()
    image_url = serializers.CharField(allow_null=True)
    product_count = serializers.IntegerField()


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
class CategorySerializer(serializers.ModelSerializer):
    # The model field is `parent` (a Category object); the API has always spoken
    # `parent_id` (an integer). `source="parent"` bridges the two, and
    # `PrimaryKeyRelatedField` checks the row exists.
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
        fields = [
            "id", "name", "description", "parent_id", "image_url",
            "sort_order", "status", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {
            "name": {"required": True, "allow_blank": False},
            "description": OPTIONAL_TEXT,
            "image_url": OPTIONAL_TEXT,
            "sort_order": {"required": False, "default": 0},
            "status": {"required": False, "default": "active"},
        }

    def validate(self, attrs):
        """Object-level validation — runs after every field passed on its own."""
        parent = attrs.get("parent")
        if parent is not None and self.instance is not None:
            if parent.pk == self.instance.pk:
                raise serializers.ValidationError("A category cannot be its own parent.")
            # Walk up from the proposed parent. Without this, A->B and B->A
            # produces a cycle that makes any recursive render hang.
            seen = {self.instance.pk}
            cursor = parent
            while cursor is not None:
                if cursor.pk in seen:
                    raise serializers.ValidationError(
                        "That parent would create a loop in the category tree."
                    )
                seen.add(cursor.pk)
                cursor = cursor.parent
        return attrs


# --------------------------------------------------------------------------
# Users (store staff / riders)
# --------------------------------------------------------------------------
class UserSerializer(serializers.ModelSerializer):
    # Overrides the ChoiceField that `choices=` on the model would generate, so
    # the error message stays the one the API has always returned.
    role = serializers.CharField()

    phone = PhoneField(
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
        max_length=12,
        trim_whitespace=False,
        help_text="Rider sign-in PIN. Omit to leave the rider unable to sign in.",
    )

    class Meta:
        model = User
        fields = [
            "id", "name", "role", "phone", "pin", "is_active", "is_available",
            "base_latitude", "base_longitude", "service_radius_km", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {
            "name": {"required": True, "allow_blank": False},
            "is_active": {"required": False, "default": True},
            "is_available": {"required": False, "default": True},
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

    def update(self, instance, validated_data):
        pin = validated_data.pop("pin", None)
        user = super().update(instance, validated_data)
        if pin:
            user.pin_hash = hash_password(pin)
            user.save(update_fields=["pin_hash"])
        return user

    def validate_role(self, value: str) -> str:
        """Raising here produces a 400.

        The exception handler prefixes the field name, so the message must not
        repeat it — the client sees
        {"detail": "role: Must be one of ['delivery', 'manager']."}.
        """
        valid = sorted(choice for choice, _ in User.ROLE_CHOICES)
        if value not in valid:
            raise serializers.ValidationError(f"Must be one of {valid}.")
        return value

    def validate_pin(self, value: str | None) -> str | None:
        """A PIN must be digits, and must not be trivially guessable.

        Rate limiting buys time against an exhaustive search; it does nothing
        against an attacker who tries "1234" against every rider in the roster.
        """
        if value is None:
            return None
        if not value.isdigit():
            raise serializers.ValidationError("Must contain digits only.")
        if len(set(value)) == 1:
            raise serializers.ValidationError("Cannot be a single repeated digit.")
        if value in {"1234", "4321", "0123", "1230", "2580"}:
            raise serializers.ValidationError("That PIN is too common. Choose another.")
        return value


class RiderSummarySerializer(serializers.ModelSerializer):
    """The rider, as shown to a customer tracking their order.

    Name and phone only. A customer needs to recognise who is at the door and
    be able to ring them; they do not need the rider's home coordinates, which
    is what `UserSerializer` would have handed over.
    """

    class Meta:
        model = User
        fields = ["id", "name", "phone"]
        read_only_fields = fields


class RiderLoginResponseSerializer(serializers.Serializer):
    access_token = serializers.CharField()
    token_type = serializers.CharField(default="bearer")
    rider = UserSerializer()


class RiderAvailabilitySerializer(serializers.Serializer):
    """The rider's own on/off switch."""

    is_available = serializers.BooleanField()


class RiderDeviceSerializer(serializers.Serializer):
    """A phone the rider app wants notifications delivered to.

    **The token is validated for shape, not just for length.** Expo issues
    `ExponentPushToken[...]`, and its gateway rejects anything else — but it
    rejects it after we have stored the row, on a background thread, in a log
    nobody is reading. Refusing it here turns a silent no-op into a 400 the app
    can report while the rider is still holding the phone.

    `FCM`/`APNs` device tokens are deliberately *not* accepted: this backend
    talks to Expo and nothing else, and a raw device token would be a value only
    a client we do not have could use.
    """

    expo_token = serializers.RegexField(
        r"^Expo(nent)?PushToken\[[^\[\]\s]+\]$",
        max_length=255,
        error_messages={
            "invalid": "That is not an Expo push token.",
        },
    )
    # Reported by the app, kept for support and never branched on. Optional
    # because it is a nicety: a phone that cannot say what it is still gets
    # notified.
    platform = serializers.ChoiceField(
        choices=[RiderDevice.IOS, RiderDevice.ANDROID],
        required=False,
        allow_blank=True,
        default="",
    )


# --------------------------------------------------------------------------
# Orders — output
# --------------------------------------------------------------------------
class OrderItemSerializer(serializers.ModelSerializer):
    # Django gives every FK a free `<name>_id` integer attribute, so these read
    # straight off the row without touching the related table.
    order_id = serializers.IntegerField(read_only=True)
    product_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = OrderItem
        fields = [
            "id", "order_id", "product_id", "quantity", "name", "price",
            "mrp", "unit", "image_url", "line_total",
        ]
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer):
    """The full internal view: what a manager, and a rider holding the order, see.

    Carries the customer's name, phone and address, so it must never be
    returned from a public endpoint. `OrderTrackingSerializer` is the public
    one, and `IncomingOrderSerializer` is what a rider sees *before* they accept.

    **No `tracking_token` here.** The token is the customer's proof of ownership
    and the sole credential on the public cancel endpoint, so anything holding it
    can cancel the order. The customer needs it, which is why it stays on
    `OrderTrackingSerializer`; a manager and a rider both act on the order by id
    and have never read it. Serialising it to them would hand a cancellation
    credential to every caller of `GET /api/orders` for no purpose.
    """

    items = OrderItemSerializer(many=True, read_only=True)
    delivery_boy_id = serializers.IntegerField(read_only=True)
    rider = RiderSummarySerializer(source="delivery_boy", read_only=True)

    status_label = serializers.CharField(source="get_status_display", read_only=True)
    delivery_type_label = serializers.CharField(
        source="get_delivery_type_display", read_only=True
    )
    promised_at = serializers.DateTimeField(read_only=True)
    minutes_remaining = serializers.IntegerField(read_only=True)
    is_late = serializers.BooleanField(read_only=True)
    fulfilment_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "customer_name", "customer_phone", "customer_address",
            "customer_landmark", "delivery_notes",
            "customer_latitude", "customer_longitude",
            "status", "status_label", "cancellation_reason",
            "items_total", "delivery_fee", "handling_fee", "grand_total",
            "payment_method",
            # What was actually collected, as opposed to what was owed. Staff-
            # facing only: these are on this serializer and deliberately not on
            # `OrderTrackingSerializer` below, because the gap between
            # `grand_total` and `amount_collected` is a matter between the store
            # and its rider. A customer who paid short does not need the app to
            # itemise it back at them, and one who paid in full learns nothing.
            "paid_at", "amount_collected", "collected_by",
            "delivery_type", "delivery_type_label",
            "promised_minutes", "promised_at", "minutes_remaining", "is_late",
            "fulfilment_minutes",
            "delivery_boy_id", "rider",
            "offered_distance_km",
            "created_at", "packed_at", "dispatched_at", "delivered_at",
            "cancelled_at",
            # Admin-only, and the console reads it to decide whether to offer
            # "Return stock to shelf": a Failed order with a null value here has
            # goods still on a bike. Deliberately absent from the customer's
            # tracking shape below — the state of the store's inventory is not
            # something a customer has any business seeing.
            "restocked_at",
            "items",
        ]
        read_only_fields = fields


class OrderTrackingSerializer(serializers.ModelSerializer):
    """What the customer sees on the tracking page.

    Reached with an unguessable token rather than an id, so the holder is
    presumed to be the customer and their own details are fair game. Everything
    internal is still absent: no rider identity, no distances, no
    cost data. The rider appears only once the order is actually dispatched —
    before that there is nobody to name.
    """

    items = OrderItemSerializer(many=True, read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    delivery_type_label = serializers.CharField(
        source="get_delivery_type_display", read_only=True
    )
    promised_at = serializers.DateTimeField(read_only=True)
    minutes_remaining = serializers.IntegerField(read_only=True)
    is_late = serializers.BooleanField(read_only=True)
    rider = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id", "tracking_token",
            "customer_name", "customer_phone", "customer_address",
            "customer_landmark", "delivery_notes",
            "status", "status_label", "cancellation_reason", "can_cancel",
            "items_total", "delivery_fee", "handling_fee", "grand_total",
            "payment_method",
            "delivery_type", "delivery_type_label",
            "promised_minutes", "promised_at", "minutes_remaining", "is_late",
            "created_at", "packed_at", "dispatched_at", "delivered_at",
            "cancelled_at",
            "rider", "items",
        ]
        read_only_fields = fields

    def get_rider(self, order: Order) -> dict | None:
        if order.status != Order.DISPATCHED or order.delivery_boy is None:
            return None
        return RiderSummarySerializer(order.delivery_boy).data

    def get_can_cancel(self, order: Order) -> bool:
        return order.status in Order.CANCELLABLE


# --------------------------------------------------------------------------
# Orders — input
# --------------------------------------------------------------------------
class CheckoutItemSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)

    def validate_quantity(self, value: int) -> int:
        limit = settings.MAX_QUANTITY_PER_ITEM
        if value > limit:
            raise serializers.ValidationError(f"At most {limit} of any one item.")
        return value


class CheckoutSerializer(serializers.Serializer):
    """A basket on its way to becoming an order.

    Note what this does *not* accept: any price, fee or total. The client sends
    product ids and quantities; every number on the resulting bill is computed
    server-side from the catalogue. A checkout that reads a total from the
    request is a checkout where the customer names their own price.
    """

    customer_name = serializers.CharField(max_length=255)
    customer_phone = PhoneField()
    customer_address = serializers.CharField(max_length=500)
    customer_landmark = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    delivery_notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    # Optional, and **not defaulted**. These used to fall back to the store's
    # own coordinates, which meant a checkout that sent no position recorded the
    # customer as standing at the counter — 0.00 km from every rider, so the
    # service-radius filter matched everyone and "nearest first" became "by id".
    # `allow_null` plus no default means an omitted position stays genuinely
    # unknown, and `validate()` below decides what that is allowed to mean.
    customer_latitude = serializers.FloatField(
        required=False, allow_null=True, min_value=-90, max_value=90
    )
    customer_longitude = serializers.FloatField(
        required=False, allow_null=True, min_value=-180, max_value=180
    )
    payment_method = serializers.ChoiceField(
        choices=[choice for choice, _ in Order.PAYMENT_CHOICES], default=Order.COD
    )
    # The tier is the one thing on this request that *does* move money, and it
    # is safe precisely because it is a closed choice: it selects a fee from the
    # server's own table rather than supplying one. An unrecognised value is a
    # 400 rather than a silent fallback — a client asking for a speed the store
    # does not sell has a bug worth surfacing.
    delivery_type = serializers.ChoiceField(
        choices=[choice for choice, _ in Order.DELIVERY_TYPE_CHOICES],
        required=False,
        default=Order.INSTANT,
    )
    items = CheckoutItemSerializer(many=True, allow_empty=False)

    def validate_customer_name(self, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter the name to deliver to.")
        return value

    def validate_customer_address(self, value: str) -> str:
        value = value.strip()
        if len(value) < 8:
            raise serializers.ValidationError(
                "Enter a full address a rider could actually find."
            )
        return value

    def validate_items(self, value: list[dict]) -> list[dict]:
        limit = settings.MAX_ITEMS_PER_ORDER
        if len(value) > limit:
            raise serializers.ValidationError(f"At most {limit} different items per order.")

        # Two lines for the same product would each be checked against stock
        # separately and could together oversell it. Merging them here means the
        # stock check downstream sees the true requested quantity.
        merged: dict[int, int] = {}
        for line in value:
            merged[line["product_id"]] = merged.get(line["product_id"], 0) + line["quantity"]

        per_item_limit = settings.MAX_QUANTITY_PER_ITEM
        for product_id, quantity in merged.items():
            if quantity > per_item_limit:
                raise serializers.ValidationError(
                    f"At most {per_item_limit} of any one item (product {product_id})."
                )

        return [{"product_id": pid, "quantity": qty} for pid, qty in merged.items()]

    def validate(self, attrs: dict) -> dict:
        """A position is optional, but half of one is not.

        Latitude without longitude is not a partial answer, it is a bug in the
        client — and treating it as "unknown" would hide that bug rather than
        report it. Both or neither.
        """
        latitude = attrs.get("customer_latitude")
        longitude = attrs.get("customer_longitude")
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError(
                "Send both customer_latitude and customer_longitude, or neither."
            )
        return attrs


class CancelOrderSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class AssignSerializer(serializers.Serializer):
    delivery_boy_id = serializers.IntegerField()


class StatusSerializer(serializers.Serializer):
    """A requested status change.

    Which transitions are *legal* is not decided here — that is
    `Order.TRANSITIONS`, checked against the order's current state, which a
    serializer cannot see. This only rejects values that are not statuses at all.
    """

    status = serializers.CharField()
    # Only read when `status` is Cancelled. The column has always existed and
    # the customer-facing cancel path has always filled it in; the console's
    # hardcoded "Cancelled by store" was the only reason a manager could not say
    # *why*, which is the one thing anyone asks afterwards.
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=255, default=""
    )
    # Only read when `status` is Delivered, and optional even then: the order's
    # own `grand_total` is the default, stamped by `Order.advance_status()`, so
    # a client that says nothing records a full collection. This field exists
    # for the case that default gets wrong — the customer who paid short.
    #
    # `min_value=0` and no `max_value`: zero is a real answer ("they took the
    # bag and paid nothing"), and the upper bound is the order's own total,
    # which a serializer cannot see. `OrderStatusView` checks that.
    amount_collected = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
        default=None,
        min_value=Decimal("0.00"),
    )

    def validate_status(self, value: str) -> str:
        valid = sorted(choice for choice, _ in Order.STATUS_CHOICES)
        if value not in valid:
            raise serializers.ValidationError(f"Must be one of {valid}.")
        return value

    def validate(self, attrs: dict) -> dict:
        # Naming an amount on a move that is not a delivery is a client bug, and
        # silently ignoring it is how a rider comes to believe they recorded a
        # collection they did not. Say so.
        if attrs.get("amount_collected") is not None and attrs["status"] != Order.DELIVERED:
            raise serializers.ValidationError(
                "amount_collected only applies when marking an order Delivered."
            )
        return attrs


# --------------------------------------------------------------------------
# Composite responses
# --------------------------------------------------------------------------
class IncomingOrderSerializer(serializers.ModelSerializer):
    """What a rider sees about an order that is **not theirs yet**.

    The offer feed lists packed orders no rider has accepted. Every available
    rider in range sees all of them, including ones they will never take — so
    this is a different audience from `OrderSerializer`, which only ever renders
    an order the rider is already carrying.

    The invariant: **nothing here identifies the customer.** No name, no phone,
    no street address, no coordinates, and above all no `tracking_token` — that
    token is the only credential on the public cancel endpoint, so publishing it
    to every rider on shift lets any of them cancel a bagged order they have
    nothing to do with. What remains is what the decision actually needs: how
    far, how big, how much cash, and how long is left.

    The address becomes `area` (see `get_area`). Everything identifying belongs
    in `OrderSerializer`, and the rider gets that the moment they accept.
    """

    item_count = serializers.SerializerMethodField()
    area = serializers.SerializerMethodField()

    promised_at = serializers.DateTimeField(read_only=True)
    minutes_remaining = serializers.IntegerField(read_only=True)
    is_late = serializers.BooleanField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            # Cash to collect. Not identifying, and the rider needs to know what
            # they are carrying before they agree to carry it.
            "grand_total", "payment_method",
            "offered_distance_km", "area", "item_count",
            "promised_at", "minutes_remaining", "is_late", "created_at",
        ]
        read_only_fields = fields

    def get_item_count(self, order: Order) -> int:
        """Total units, not lines — "will this fit on the bike?"

        Reads through the `prefetch_related("items")` the dashboard queryset
        already carries, so this costs no extra query.
        """
        return sum(item.quantity for item in order.items.all())

    def get_area(self, order: Order) -> str:
        """Roughly where, without saying exactly where.

        A judgement call, so it is written down: a rider deciding whether to
        take a drop needs the neighbourhood, and the full street address would
        let anyone on shift collect the delivery addresses of the whole town by
        polling. The landmark is preferred because the checkout form asks for it
        precisely as a coarse locator; otherwise the last comma-separated
        segment of the address is the closest thing to a locality, and the house
        number is always in an earlier segment.
        """
        if order.customer_landmark:
            return order.customer_landmark
        tail = order.customer_address.rsplit(",", 1)[-1].strip()
        # A single-segment address has no locality to isolate, and returning the
        # whole thing would defeat the point of this method.
        return tail if tail and tail != order.customer_address.strip() else ""


class DeliveryDashboardSerializer(serializers.Serializer):
    """A composite response with no model of its own.

    Two different order serializers on purpose. `incoming_orders` are offers —
    orders belonging to nobody, shown to every available rider in range — so
    they carry no customer detail. `active_order` and `recent_orders` are the
    rider's own work, and they need the address and phone to deliver it.
    """

    incoming_orders = IncomingOrderSerializer(many=True)
    active_order = OrderSerializer(allow_null=True)
    recent_orders = OrderSerializer(many=True)
    is_available = serializers.BooleanField()


class DeliveryTierSerializer(serializers.Serializer):
    """One delivery speed, as the storefront's picker renders it.

    The fee travels with the tier rather than being looked up separately,
    because the picker's whole job is to show a customer what each option costs
    them — and two options priced from two different places is how a cart ends
    up disagreeing with its own checkout.
    """

    key = serializers.CharField()
    label = serializers.CharField()
    fee = serializers.DecimalField(max_digits=10, decimal_places=2)
    promise_minutes = serializers.IntegerField()


class StoreConfigSerializer(serializers.Serializer):
    """Everything the storefront needs to render its promise and its fees.

    Served so the delivery fees, the free-delivery threshold and the promised
    times are stated by the same source of truth that will charge for them. A
    hardcoded "₹15 delivery" in the React app is a number that goes stale the
    day someone edits the environment.
    """

    store_name = serializers.CharField()
    store_city = serializers.CharField()
    delivery_tiers = DeliveryTierSerializer(many=True)
    free_delivery_above = serializers.DecimalField(max_digits=10, decimal_places=2)
    handling_fee = serializers.DecimalField(max_digits=10, decimal_places=2)
    min_order_value = serializers.DecimalField(max_digits=10, decimal_places=2)

    # The default tier's fee and window, kept as flat fields. Everything that
    # asks "what does this store promise?" without caring about the choice — the
    # page title, the header strapline, an older client mid-deploy — reads these
    # and does not have to learn about tiers.
    promise_minutes = serializers.IntegerField()
    delivery_fee = serializers.DecimalField(max_digits=10, decimal_places=2)

    # --- whether the shop will actually take an order right now ------------
    # The storefront needs this *before* the customer fills in an address, so it
    # can say "we open at 07:00" on the cart rather than accepting a basket and
    # refusing it at the last step. `closed_reason` is the same sentence the
    # checkout endpoint would answer with, produced by the same method, so the
    # two can never disagree.
    is_open = serializers.BooleanField()
    closed_reason = serializers.CharField(allow_blank=True)
    opens_at = serializers.TimeField()
    closes_at = serializers.TimeField()

    # The delivery area, so the storefront can check a captured position before
    # it is sent and say so on the address form instead of at checkout.
    delivery_radius_km = serializers.FloatField()
    store_latitude = serializers.FloatField()
    store_longitude = serializers.FloatField()


class StoreSettingsSerializer(serializers.ModelSerializer):
    """The operational knobs, read and written by the console.

    Separate from `StoreConfigSerializer` for the same reason `ProductSerializer`
    is separate from `StoreProductSerializer`: one is what a customer may see,
    the other is what a manager may change. Overlapping fields is fine;
    conflating the classes is how a write field ends up on a public endpoint.
    """

    class Meta:
        model = StoreSettings
        fields = [
            "is_accepting_orders",
            "closed_message",
            "opens_at",
            "closes_at",
            "delivery_radius_km",
            "store_latitude",
            "store_longitude",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]
        # Not OPTIONAL_TEXT: that carries `allow_null`, and this column is
        # `blank=True, default=""` rather than nullable, so a null would pass
        # the serializer and fail at the database. Blank is the empty state
        # here, and it means "use the generic message".
        extra_kwargs = {
            "closed_message": {"required": False, "allow_blank": True},
        }

    def validate_delivery_radius_km(self, value: float) -> float:
        if value <= 0:
            raise serializers.ValidationError("The delivery radius must be positive.")
        # Not a physical limit — a typo guard. Aizawl's whole urban area is
        # inside ~15 km, so a three-digit radius is a slipped decimal point, and
        # the cost of accepting it is a 15-minute promise made to another state.
        if value > 100:
            raise serializers.ValidationError(
                "A radius over 100 km is almost certainly a mistake."
            )
        return value


class BasketLineSerializer(serializers.Serializer):
    """One priced row of a quoted basket.

    This exists because the rule above was being broken for want of a field.
    The quote returned four totals and no breakdown, so a cart listing five
    products had nowhere to get "₹35.00 x 2" from and multiplied it in
    TypeScript — the second pricing engine the whole design forbids.

    `Basket.lines` already carries `line_total` as a Decimal quantised by
    `money()`, and the view was discarding it. Nothing new is computed here; it
    is arithmetic the server already did, finally being handed over.
    """

    product_id = serializers.IntegerField()
    name = serializers.CharField()
    quantity = serializers.IntegerField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2)


class BasketQuoteSerializer(serializers.Serializer):
    """What a basket would cost, without placing an order.

    The storefront calls this to show the bill in the cart drawer. It exists so
    the totals a customer sees before paying are produced by the same code that
    produces the totals they are charged — not by a parallel implementation in
    TypeScript that drifts the first time a fee changes.
    """

    items_total = serializers.DecimalField(max_digits=10, decimal_places=2)
    delivery_fee = serializers.DecimalField(max_digits=10, decimal_places=2)
    handling_fee = serializers.DecimalField(max_digits=10, decimal_places=2)
    grand_total = serializers.DecimalField(max_digits=10, decimal_places=2)
    free_delivery_shortfall = serializers.DecimalField(max_digits=10, decimal_places=2)
    meets_minimum = serializers.BooleanField()
    unavailable = serializers.ListField(child=serializers.DictField(), default=list)

    # The per-row breakdown, so no client has to multiply a price by a quantity.
    lines = BasketLineSerializer(many=True, default=list)

    # Echoed back so the cart shows the tier the bill was actually priced at,
    # rather than the one the UI believes it asked for. If a request is dropped
    # or arrives out of order, this is what makes the discrepancy visible
    # instead of silently showing one tier's ETA above another tier's total.
    delivery_type = serializers.CharField()
    promised_minutes = serializers.IntegerField()

    @staticmethod
    def build(items_total, charges, unavailable: list[dict], tier, lines=()) -> dict:
        return {
            **charges.as_dict(),
            "free_delivery_shortfall": free_delivery_shortfall(items_total),
            "meets_minimum": money(items_total) >= money(settings.MIN_ORDER_VALUE),
            "unavailable": unavailable,
            "lines": [
                {
                    "product_id": line.product.id,
                    "name": line.product.name,
                    "quantity": line.quantity,
                    "price": money(line.product.price),
                    "line_total": line.line_total,
                }
                for line in lines
            ],
            "delivery_type": tier.key,
            "promised_minutes": tier.promise_minutes,
        }


# --------------------------------------------------------------------------
# Small shared shapes
# --------------------------------------------------------------------------
class UploadResponseSerializer(serializers.Serializer):
    image_url = serializers.CharField()


class SuccessSerializer(serializers.Serializer):
    """`{"success": true}` — what the delete and reject endpoints return."""

    success = serializers.BooleanField(default=True)


# --------------------------------------------------------------------------
# Console accounts (Admin-only surface)
# --------------------------------------------------------------------------
class AdminUserSerializer(serializers.ModelSerializer):
    """An admin-console login. Read and written only by `/api/admins`.

    `password` is write-only and hashed on the way in, exactly as `UserSerializer`
    treats a rider's `pin` — `password_hash` never appears in `fields`, so it
    cannot leak through a forgotten exclusion.

    It is optional on update and required on create. That asymmetry is the point:
    editing someone's role must not force you to know or reset their password,
    and a missing `password` on a PUT leaves the stored hash untouched.
    """

    email = serializers.EmailField(
        validators=[
            UniqueValidator(
                queryset=AdminUser.objects.all(),
                message="An account with that email already exists.",
            )
        ]
    )
    name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    role = serializers.ChoiceField(choices=AdminUser.ROLE_CHOICES)
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_null=True,
        min_length=8,
        max_length=128,
        # Never strip: a password may legitimately begin or end with a space,
        # and trimming it here would make the stored hash disagree with what the
        # user typed at login.
        trim_whitespace=False,
    )

    class Meta:
        model = AdminUser
        fields = ["id", "email", "name", "role", "password", "is_active",
                  "created_at", "last_login_at"]
        read_only_fields = ["id", "created_at", "last_login_at"]

    def validate_email(self, value: str) -> str:
        # Login lowercases before lookup, so storage must lowercase too or an
        # account created as "Owner@x.com" could never sign in.
        return value.strip().lower()

    def validate_password(self, value: str | None) -> str | None:
        if value is None:
            return value
        if value.strip() == "":
            raise serializers.ValidationError("Password cannot be blank.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        if not password:
            raise serializers.ValidationError(
                {"password": "A password is required when creating an account."}
            )
        validated_data["password_hash"] = hash_password(password)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        if password:
            instance.password_hash = hash_password(password)
        return super().update(instance, validated_data)


class AuditLogSerializer(serializers.ModelSerializer):
    """Read-only. Nothing writes an audit row through the API — see api/audit.py."""

    action_label = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = AuditLog
        fields = ["id", "actor_kind", "actor_id", "actor_label", "actor_role",
                  "action", "action_label", "entity", "entity_id", "summary",
                  "changes", "created_at"]
        read_only_fields = fields


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------
# These are output-only `Serializer`s rather than ModelSerializers: every figure
# below is an aggregate over many rows, so there is no model to mirror. They
# exist mainly so drf-spectacular documents the shapes the console consumes.
class MetricSerializer(serializers.Serializer):
    """One headline figure, with the same figure for the preceding period.

    The comparison is carried rather than computed on the client, because
    "previous period" has to mean the window of equal length immediately before
    this one — and two clients would define that two ways.
    """

    value = serializers.DecimalField(max_digits=12, decimal_places=2)
    previous = serializers.DecimalField(max_digits=12, decimal_places=2)


class AnalyticsSummarySerializer(serializers.Serializer):
    revenue = MetricSerializer()
    orders = MetricSerializer()
    average_order_value = MetricSerializer()
    on_time_rate = MetricSerializer()
    cancellation_rate = MetricSerializer()
    from_date = serializers.DateField()
    to_date = serializers.DateField()


class RevenuePointSerializer(serializers.Serializer):
    date = serializers.DateField()
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2)
    orders = serializers.IntegerField()


class TopProductSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(allow_null=True)
    name = serializers.CharField()
    units = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2)


class CategoryShareSerializer(serializers.Serializer):
    category = serializers.CharField()
    units = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2)


class RiderPerformanceSerializer(serializers.Serializer):
    rider_id = serializers.IntegerField()
    name = serializers.CharField()
    delivered = serializers.IntegerField()
    late = serializers.IntegerField()
    average_minutes = serializers.FloatField(allow_null=True)


class DeliveryPerformanceSerializer(serializers.Serializer):
    delivered = serializers.IntegerField()
    late = serializers.IntegerField()
    on_time_rate = serializers.FloatField()
    average_minutes = serializers.FloatField(allow_null=True)
    riders = RiderPerformanceSerializer(many=True)


class CashRiderSerializer(serializers.Serializer):
    """One rider's till position for the window.

    `rider_id` and `name` are nullable together: orders an admin closed from the
    console have no rider at a door, and they still hold cash the store has to
    account for, so they are reported as one unattributed row rather than
    dropped. A missing row is how money goes missing quietly.
    """

    rider_id = serializers.IntegerField(allow_null=True)
    name = serializers.CharField(allow_null=True)
    orders = serializers.IntegerField()
    expected = serializers.DecimalField(max_digits=12, decimal_places=2)
    collected = serializers.DecimalField(max_digits=12, decimal_places=2)
    shortfall = serializers.DecimalField(max_digits=12, decimal_places=2)
    short_orders = serializers.IntegerField()


class CashDaySerializer(serializers.Serializer):
    day = serializers.DateField()
    orders = serializers.IntegerField()
    expected = serializers.DecimalField(max_digits=12, decimal_places=2)
    collected = serializers.DecimalField(max_digits=12, decimal_places=2)
    shortfall = serializers.DecimalField(max_digits=12, decimal_places=2)


class CashReconciliationSerializer(serializers.Serializer):
    orders = serializers.IntegerField()
    expected = serializers.DecimalField(max_digits=12, decimal_places=2)
    collected = serializers.DecimalField(max_digits=12, decimal_places=2)
    shortfall = serializers.DecimalField(max_digits=12, decimal_places=2)
    short_orders = serializers.IntegerField()
    riders = CashRiderSerializer(many=True)
    days = CashDaySerializer(many=True)


class InventoryHealthSerializer(serializers.Serializer):
    total_products = serializers.IntegerField()
    active_products = serializers.IntegerField()
    out_of_stock = serializers.IntegerField()
    low_stock = serializers.IntegerField()
    stock_units = serializers.IntegerField()
    # Valued at cost, not at price: this answers "what is sitting on the shelf
    # worth to us", which is a purchasing question, not a sales one.
    stock_value = serializers.DecimalField(max_digits=12, decimal_places=2)
    items = TopProductSerializer(many=True)
