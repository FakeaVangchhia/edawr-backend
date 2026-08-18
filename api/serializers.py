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

from api.models import AdminUser, AuditLog, Category, Order, OrderItem, Product, User
from api.pricing import free_delivery_shortfall, money
from api.security import hash_password
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
    offered_to_delivery_boy_id = serializers.IntegerField(read_only=True)
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
            "delivery_type", "delivery_type_label",
            "promised_minutes", "promised_at", "minutes_remaining", "is_late",
            "fulfilment_minutes",
            "delivery_boy_id", "offered_to_delivery_boy_id", "rider",
            "offered_distance_km",
            "created_at", "packed_at", "dispatched_at", "delivered_at",
            "cancelled_at",
            "items",
        ]
        read_only_fields = fields


class OrderTrackingSerializer(serializers.ModelSerializer):
    """What the customer sees on the tracking page.

    Reached with an unguessable token rather than an id, so the holder is
    presumed to be the customer and their own details are fair game. Everything
    internal is still absent: no `offered_to_delivery_boy_id`, no distances, no
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
    customer_latitude = serializers.FloatField(required=False, min_value=-90, max_value=90, default=23.7272)
    customer_longitude = serializers.FloatField(required=False, min_value=-180, max_value=180, default=92.7178)
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

    def validate_status(self, value: str) -> str:
        valid = sorted(choice for choice, _ in Order.STATUS_CHOICES)
        if value not in valid:
            raise serializers.ValidationError(f"Must be one of {valid}.")
        return value


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

    # Echoed back so the cart shows the tier the bill was actually priced at,
    # rather than the one the UI believes it asked for. If a request is dropped
    # or arrives out of order, this is what makes the discrepancy visible
    # instead of silently showing one tier's ETA above another tier's total.
    delivery_type = serializers.CharField()
    promised_minutes = serializers.IntegerField()

    @staticmethod
    def build(items_total, charges, unavailable: list[dict], tier) -> dict:
        return {
            **charges.as_dict(),
            "free_delivery_shortfall": free_delivery_shortfall(items_total),
            "meets_minimum": money(items_total) >= money(settings.MIN_ORDER_VALUE),
            "unavailable": unavailable,
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
