import re
from contextlib import asynccontextmanager
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from uuid import uuid4

import socketio
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .database import Base, SessionLocal, engine, get_db
from .models import Message, Order, OrderItem, Product, User
from .schemas import (
    AssignOrderIn,
    CreateProductIn,
    DeliveryDashboardOut,
    DeliveryDecisionIn,
    MessageOut,
    OrderItemOut,
    OrderOut,
    ProductOut,
    SendMessageIn,
    UpdateOrderStatusIn,
    UserOut,
    WhatsAppWebhookIn,
)
from .seed import seed_initial_data


settings = get_settings()
UPLOADS_DIR = Path(__file__).resolve().parents[1] / "uploads"
PRODUCT_UPLOADS_DIR = UPLOADS_DIR / "products"
PRODUCT_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=settings.cors_origins)


def serialize_message(message: Message) -> dict:
    return MessageOut.model_validate(message).model_dump(mode="json")


def serialize_product(product: Product) -> dict:
    payload = ProductOut.model_validate(product).model_dump(mode="json")
    payload["price"] = float(product.price)
    payload["cost_price"] = float(product.cost_price)
    payload["mrp"] = float(product.mrp)
    return payload


def serialize_order(order: Order) -> dict:
    items = [
        OrderItemOut(
            id=item.id,
            order_id=item.order_id,
            product_id=item.product_id,
            quantity=item.quantity,
            name=item.product.name,
            price=float(item.product.price),
        ).model_dump(mode="json")
        for item in order.items
    ]
    return OrderOut(
        id=order.id,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        customer_address=order.customer_address,
        customer_latitude=order.customer_latitude,
        customer_longitude=order.customer_longitude,
        status=order.status,
        delivery_boy_id=order.delivery_boy_id,
        offered_to_delivery_boy_id=order.offered_to_delivery_boy_id,
        offered_distance_km=order.offered_distance_km,
        created_at=order.created_at,
        items=items,
    ).model_dump(mode="json")


def get_order_or_404(db: Session, order_id: int) -> Order:
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def build_whatsapp_reply(order_id: int) -> str:
    return f"Order placed successfully! Your Order ID is #{order_id}. We will notify you once it's assigned."


def ensure_product_columns(db: Session) -> None:
    statements = [
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS sku VARCHAR(100) NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS category VARCHAR(100) NOT NULL DEFAULT 'General'",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS brand VARCHAR(100) NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS unit VARCHAR(50) NOT NULL DEFAULT 'unit'",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_price NUMERIC(10, 2) NOT NULL DEFAULT 0",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS mrp NUMERIC(10, 2) NOT NULL DEFAULT 0",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS reorder_level INTEGER NOT NULL DEFAULT 10",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'Active'",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS location VARCHAR(100) NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_name VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_phone VARCHAR(50) NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url TEXT NOT NULL DEFAULT ''",
    ]
    for statement in statements:
        db.execute(text(statement))
    db.commit()


def ensure_user_columns(db: Session) -> None:
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS base_latitude DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS base_longitude DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS service_radius_km DOUBLE PRECISION NOT NULL DEFAULT 5",
    ]
    for statement in statements:
        db.execute(text(statement))
    db.commit()


def ensure_order_columns(db: Session) -> None:
    statements = [
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255) NOT NULL DEFAULT 'Customer'",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_address VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_latitude DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_longitude DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS offered_to_delivery_boy_id INTEGER NULL",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS offered_distance_km DOUBLE PRECISION NULL",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS rejected_delivery_boy_ids TEXT NOT NULL DEFAULT ''",
    ]
    for statement in statements:
        db.execute(text(statement))
    db.commit()


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = sin(dlat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2) ** 2
    return 6371 * 2 * asin(sqrt(a))


def parse_rejections(raw_value: str) -> set[int]:
    return {int(value) for value in raw_value.split(",") if value.strip().isdigit()}


def serialize_rejections(values: set[int]) -> str:
    return ",".join(str(value) for value in sorted(values))


def active_assignment_count(db: Session, delivery_boy_id: int) -> int:
    return db.scalar(
        select(func.count(Order.id)).where(
            Order.delivery_boy_id == delivery_boy_id,
            Order.status == "Assigned",
        )
    ) or 0


def rank_delivery_candidates(
    db: Session, order: Order, excluded_user_ids: set[int] | None = None
) -> list[tuple[User, float, int]]:
    excluded_user_ids = excluded_user_ids or set()
    riders = db.scalars(select(User).where(User.role == "delivery").order_by(User.id.asc())).all()
    ranked: list[tuple[User, float, int]] = []
    for rider in riders:
        if rider.id in excluded_user_ids:
            continue
        if rider.base_latitude == 0 and rider.base_longitude == 0:
            continue

        distance = haversine_distance_km(
            rider.base_latitude,
            rider.base_longitude,
            order.customer_latitude,
            order.customer_longitude,
        )
        if distance > rider.service_radius_km:
            continue

        ranked.append((rider, distance, active_assignment_count(db, rider.id)))

    ranked.sort(key=lambda item: (item[2], item[1], item[0].id))
    return ranked


def dispatch_order_to_best_rider(db: Session, order: Order) -> Order:
    if order.status != "Pending" or order.delivery_boy_id is not None:
        return order

    excluded_user_ids = parse_rejections(order.rejected_delivery_boy_ids)
    ranked = rank_delivery_candidates(db, order, excluded_user_ids)
    if not ranked:
        order.offered_to_delivery_boy_id = None
        order.offered_distance_km = None
        db.commit()
        db.refresh(order)
        return order

    selected_rider, distance_km, _ = ranked[0]
    order.offered_to_delivery_boy_id = selected_rider.id
    order.offered_distance_km = round(distance_km, 1)
    db.commit()
    db.refresh(order)
    return order


def dispatch_pending_orders(db: Session) -> None:
    pending_orders = db.scalars(
        select(Order)
        .where(Order.status == "Pending")
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    ).all()
    for order in pending_orders:
        if order.offered_to_delivery_boy_id is None:
            dispatch_order_to_best_rider(db, order)


def parse_order_message(db: Session, message: str) -> tuple[list[tuple[Product, int]], bool]:
    parsed_items: list[tuple[Product, int]] = []
    parse_error = False

    for raw_item in [segment.strip() for segment in message.split(",") if segment.strip()]:
        match = re.match(r"^(\d+)\s+(.+)$", raw_item, flags=re.IGNORECASE)
        if not match:
            parse_error = True
            continue

        quantity = int(match.group(1))
        product_name = match.group(2).strip().lower()
        product = db.scalar(select(Product).where(func.lower(Product.name).contains(product_name)))
        if product is None or product.stock < quantity:
            parse_error = True
            continue

        parsed_items.append((product, quantity))

    return parsed_items, parse_error


def infer_customer_location(phone: str) -> tuple[str, float, float]:
    last_digit = int(phone[-1]) if phone and phone[-1].isdigit() else 0
    demo_locations = [
        ("Richmond Road, Bengaluru", 12.9647, 77.6092),
        ("Koramangala 5th Block, Bengaluru", 12.9349, 77.6207),
        ("Indiranagar 100 Feet Road, Bengaluru", 12.9784, 77.6408),
        ("Malleshwaram 8th Cross, Bengaluru", 13.0035, 77.5691),
        ("HSR Layout Sector 2, Bengaluru", 12.9116, 77.6474),
    ]
    return demo_locations[last_digit % len(demo_locations)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_user_columns(db)
        ensure_order_columns(db)
        ensure_product_columns(db)
        seed_initial_data(db)
        dispatch_pending_orders(db)
    yield


api = FastAPI(title="AirO Backend", lifespan=lifespan)
api.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
api.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.get("/")
async def root() -> dict[str, str]:
    return {"message": "Backend API is running."}


@api.get("/api/messages")
async def get_messages(db: Session = Depends(get_db)) -> list[dict]:
    messages = db.scalars(select(Message).order_by(Message.created_at.asc())).all()
    return [serialize_message(message) for message in messages]


@api.post("/api/messages/send")
async def send_message(payload: SendMessageIn, db: Session = Depends(get_db)) -> dict:
    message = Message(phone=payload.phone, direction="outbound", content=payload.message)
    db.add(message)
    db.commit()
    db.refresh(message)
    response = serialize_message(message)
    await sio.emit("message:new", response)
    return response


@api.post("/api/uploads/products/image")
async def upload_product_image(file: UploadFile = File(...)) -> dict[str, str]:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Please upload an image file")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image must be 5 MB or smaller")

    suffix = Path(file.filename or "upload").suffix.lower() or ".jpg"
    filename = f"{uuid4().hex}{suffix}"
    destination = PRODUCT_UPLOADS_DIR / filename
    destination.write_bytes(content)
    return {"image_url": f"/uploads/products/{filename}"}


@api.get("/api/products")
async def get_products(db: Session = Depends(get_db)) -> list[dict]:
    products = db.scalars(select(Product).order_by(Product.id.asc())).all()
    return [serialize_product(product) for product in products]


@api.post("/api/products")
async def create_product(payload: CreateProductIn, db: Session = Depends(get_db)) -> dict:
    product = Product(
        name=payload.name,
        sku=payload.sku,
        category=payload.category,
        brand=payload.brand,
        unit=payload.unit,
        price=payload.price,
        cost_price=payload.cost_price,
        mrp=payload.mrp,
        stock=payload.stock,
        reorder_level=payload.reorder_level,
        status=payload.status,
        location=payload.location,
        supplier_name=payload.supplier_name,
        supplier_phone=payload.supplier_phone,
        description=payload.description,
        image_url=payload.image_url,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    response = serialize_product(product)
    await sio.emit("product:updated", response)
    return response


@api.put("/api/products/{product_id}")
async def update_product(product_id: int, payload: CreateProductIn, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    product.name = payload.name
    product.sku = payload.sku
    product.category = payload.category
    product.brand = payload.brand
    product.unit = payload.unit
    product.price = payload.price
    product.cost_price = payload.cost_price
    product.mrp = payload.mrp
    product.stock = payload.stock
    product.reorder_level = payload.reorder_level
    product.status = payload.status
    product.location = payload.location
    product.supplier_name = payload.supplier_name
    product.supplier_phone = payload.supplier_phone
    product.description = payload.description
    product.image_url = payload.image_url

    db.commit()
    db.refresh(product)
    response = serialize_product(product)
    await sio.emit("product:updated", response)
    return response


@api.get("/api/users")
async def get_users(db: Session = Depends(get_db)) -> list[dict]:
    users = db.scalars(select(User).order_by(User.id.asc())).all()
    return [UserOut.model_validate(user).model_dump(mode="json") for user in users]


@api.get("/api/orders")
async def get_orders(db: Session = Depends(get_db)) -> list[dict]:
    orders = db.scalars(
        select(Order)
        .order_by(Order.created_at.desc())
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    ).all()
    return [serialize_order(order) for order in orders]


@api.get("/api/delivery/{delivery_boy_id}/dashboard")
async def get_delivery_dashboard(delivery_boy_id: int, db: Session = Depends(get_db)) -> dict:
    rider = db.get(User, delivery_boy_id)
    if rider is None or rider.role != "delivery":
        raise HTTPException(status_code=404, detail="Delivery user not found")

    dispatch_pending_orders(db)

    incoming_orders = db.scalars(
        select(Order)
        .where(
            Order.status == "Pending",
            Order.offered_to_delivery_boy_id == delivery_boy_id,
        )
        .order_by(Order.created_at.asc())
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    ).all()
    active_order = db.scalar(
        select(Order)
        .where(
            Order.status == "Assigned",
            Order.delivery_boy_id == delivery_boy_id,
        )
        .order_by(Order.created_at.asc())
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    )
    recent_orders = db.scalars(
        select(Order)
        .where(
            Order.status == "Delivered",
            Order.delivery_boy_id == delivery_boy_id,
        )
        .order_by(Order.created_at.desc())
        .limit(10)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
    ).all()

    return DeliveryDashboardOut(
        incoming_orders=[OrderOut.model_validate(serialize_order(order)) for order in incoming_orders],
        active_order=OrderOut.model_validate(serialize_order(active_order)) if active_order else None,
        recent_orders=[OrderOut.model_validate(serialize_order(order)) for order in recent_orders],
    ).model_dump(mode="json")


@api.patch("/api/orders/{order_id}/status")
async def update_order_status(
    order_id: int, payload: UpdateOrderStatusIn, db: Session = Depends(get_db)
) -> dict:
    order = get_order_or_404(db, order_id)
    order.status = payload.status
    if payload.status == "Delivered":
        order.offered_to_delivery_boy_id = None
        order.offered_distance_km = None
    db.commit()
    db.refresh(order)
    db.refresh(order, attribute_names=["items"])
    order = get_order_or_404(db, order_id)
    response = serialize_order(order)
    await sio.emit("order:updated", response)
    return response


@api.post("/api/orders/{order_id}/assign")
async def assign_order(order_id: int, payload: AssignOrderIn, db: Session = Depends(get_db)) -> dict:
    order = get_order_or_404(db, order_id)
    delivery_user = db.get(User, payload.delivery_boy_id)
    if delivery_user is None:
        raise HTTPException(status_code=404, detail="Delivery user not found")

    order.delivery_boy_id = payload.delivery_boy_id
    order.status = "Assigned"
    order.offered_to_delivery_boy_id = None
    order.offered_distance_km = None
    db.commit()
    order = get_order_or_404(db, order_id)
    response = serialize_order(order)
    await sio.emit("order:updated", response)
    return response


@api.post("/api/orders/{order_id}/accept")
async def accept_order_offer(order_id: int, payload: DeliveryDecisionIn, db: Session = Depends(get_db)) -> dict:
    order = get_order_or_404(db, order_id)
    if order.status != "Pending":
        raise HTTPException(status_code=409, detail="Order is no longer available")
    if order.offered_to_delivery_boy_id != payload.delivery_boy_id:
        raise HTTPException(status_code=409, detail="Order is not currently offered to this rider")

    order.delivery_boy_id = payload.delivery_boy_id
    order.status = "Assigned"
    order.offered_to_delivery_boy_id = None
    db.commit()
    order = get_order_or_404(db, order_id)
    response = serialize_order(order)
    await sio.emit("order:updated", response)
    return response


@api.post("/api/orders/{order_id}/reject")
async def reject_order_offer(order_id: int, payload: DeliveryDecisionIn, db: Session = Depends(get_db)) -> dict:
    order = get_order_or_404(db, order_id)
    if order.status != "Pending":
        raise HTTPException(status_code=409, detail="Order is no longer pending")
    if order.offered_to_delivery_boy_id != payload.delivery_boy_id:
        raise HTTPException(status_code=409, detail="Order is not currently offered to this rider")

    rejected_ids = parse_rejections(order.rejected_delivery_boy_ids)
    rejected_ids.add(payload.delivery_boy_id)
    order.rejected_delivery_boy_ids = serialize_rejections(rejected_ids)
    order.offered_to_delivery_boy_id = None
    order.offered_distance_km = None
    db.commit()

    order = get_order_or_404(db, order_id)
    dispatch_order_to_best_rider(db, order)
    order = get_order_or_404(db, order_id)
    response = serialize_order(order)
    await sio.emit("order:updated", response)
    return response


@api.post("/api/webhook/whatsapp")
async def whatsapp_webhook(payload: WhatsAppWebhookIn, db: Session = Depends(get_db)) -> dict:
    inbound_message = Message(phone=payload.phone, direction="inbound", content=payload.message)
    db.add(inbound_message)
    db.commit()
    db.refresh(inbound_message)
    await sio.emit("message:new", serialize_message(inbound_message))

    parsed_items, parse_error = parse_order_message(db, payload.message)
    if not parsed_items or parse_error:
        fallback = Message(
            phone=payload.phone,
            direction="outbound",
            content=(
                "We couldn't parse your order or items are out of stock. "
                "Please use format: '2 milk, 1 bread'. Available: Milk, Bread, Eggs."
            ),
        )
        db.add(fallback)
        db.commit()
        db.refresh(fallback)
        await sio.emit("message:new", serialize_message(fallback))
        return {"success": True, "message": "Fallback sent"}

    address, latitude, longitude = infer_customer_location(payload.phone)
    order = Order(
        customer_name=f"Customer {payload.phone[-4:]}",
        customer_phone=payload.phone,
        customer_address=address,
        customer_latitude=latitude,
        customer_longitude=longitude,
        status="Pending",
    )
    db.add(order)
    db.flush()

    for product, quantity in parsed_items:
        db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity))
        product.stock -= quantity

    confirmation = Message(
        phone=payload.phone,
        direction="outbound",
        content=build_whatsapp_reply(order.id),
    )
    db.add(confirmation)
    db.commit()
    db.refresh(confirmation)

    full_order = get_order_or_404(db, order.id)
    dispatch_order_to_best_rider(db, full_order)
    full_order = get_order_or_404(db, order.id)
    order_payload = serialize_order(full_order)
    await sio.emit("order:created", order_payload)
    await sio.emit("inventory:updated")
    await sio.emit("message:new", serialize_message(confirmation))

    return {
        "success": True,
        "order": order_payload,
        "message": "Order placed successfully!",
    }


app = socketio.ASGIApp(socketio_server=sio, other_asgi_app=api, socketio_path="socket.io")
