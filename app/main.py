import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from uuid import uuid4

import httpx
import socketio
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status, Request as FastAPIRequest, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from .auth import create_access_token, get_current_admin, verify_password
from .config import get_settings
from .database import Base, SessionLocal, engine, get_db
from .models import AdminCredential, Category, Message, Order, OrderItem, OrderRejection, Product, User
from .schemas import (
    AdminLoginIn,
    AssignOrderIn,
    CategoryOut,
    CreateCategoryIn,
    CreateProductIn,
    DeliveryDashboardOut,
    DeliveryDecisionIn,
    MessageOut,
    OrderItemOut,
    OrderOut,
    ProductOut,
    SendMessageIn,
    StartWhatsAppTemplateIn,
    TokenOut,
    UpdateOrderStatusIn,
    UserOut,
)
from .seed import seed_admin_credentials, seed_initial_data


settings = get_settings()
UPLOADS_DIR = Path(__file__).resolve().parents[1] / "uploads"
PRODUCT_UPLOADS_DIR = UPLOADS_DIR / "products"
PRODUCT_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=settings.cors_origins)


def _check_image_magic(content: bytes) -> bool:
    """Validate file content against known image magic bytes."""
    if content[:3] == b"\xff\xd8\xff":
        return True  # JPEG
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return True  # PNG
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return True  # GIF
    if content[:4] == b"RIFF" and len(content) >= 12 and content[8:12] == b"WEBP":
        return True  # WebP
    return False


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
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.rejections),
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def build_whatsapp_reply(order_id: int) -> str:
    return (
        f"Order placed successfully! Your Order ID is #{order_id}. "
        "We will notify you once it's assigned."
    )


def build_product_catalog_message(db: Session) -> str:
    active_products = db.scalars(
        select(Product)
        .where(Product.status == "Active", Product.stock > 0)
        .order_by(Product.name.asc())
    ).all()
    if not active_products:
        return "We currently have no items in stock. Please check back later!"

    category_map: dict[str, list[Product]] = {}
    for product in active_products:
        cat_name = product.category_name or "General"
        category_map.setdefault(cat_name, []).append(product)

    lines = ["Welcome to eDawr. Here is our available product list:", "", "*Available Products:*"]
    for category, products in category_map.items():
        lines.append("")
        lines.append(f"_{category}_:")
        for product in products:
            lines.append(f"- {product.name} - Rs.{float(product.price):.2f} ({product.stock} in stock)")

    lines.append("")
    lines.append("Reply with quantities, for example: 2 Milk, 1 Bread")
    return "\n".join(lines)


async def _whatsapp_post(url: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.whatsapp_access_token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return response.json() if response.content else {"success": True}


async def send_whatsapp_template(phone: str) -> dict:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        raise HTTPException(
            status_code=503,
            detail="WhatsApp Cloud API is not configured. Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID.",
        )
    url = f"https://graph.facebook.com/v22.0/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": settings.whatsapp_template_name,
            "language": {"code": settings.whatsapp_template_language},
        },
    }
    try:
        return await _whatsapp_post(url, payload)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"WhatsApp API error: {exc.response.text}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Unable to reach WhatsApp API: {exc}") from exc


async def send_whatsapp_text(phone: str, message_text: str) -> dict:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return {"success": True, "simulated": True}
    url = f"https://graph.facebook.com/v22.0/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": message_text},
    }
    try:
        return await _whatsapp_post(url, payload)
    except Exception:
        return {"success": False}


async def send_whatsapp_interactive_button(
    phone: str, message_text: str, button_id: str, button_title: str
) -> dict:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return {"success": True, "simulated": True}
    url = f"https://graph.facebook.com/v22.0/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": message_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": button_id, "title": button_title}}
                ]
            },
        },
    }
    try:
        return await _whatsapp_post(url, payload)
    except Exception:
        return {"success": False}


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = sin(dlat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2) ** 2
    return 6371 * 2 * asin(sqrt(a))


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
    if order.customer_latitude == 0 and order.customer_longitude == 0:
        return order  # Cannot dispatch without customer location

    rejected_ids = {r.delivery_boy_id for r in order.rejections}
    ranked = rank_delivery_candidates(db, order, rejected_ids)
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
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.rejections),
        )
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_initial_data(db)
        seed_admin_credentials(db)
        dispatch_pending_orders(db)
    yield


api = FastAPI(title="eDawr Backend", lifespan=lifespan)
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


@api.get("/health", tags=["Health"])
async def health_check() -> dict:
    return {"status": "ok", "service": "eDawr Backend"}


# ── Auth ─────────────────────────────────────────────────────────────────────

@api.post("/api/admin/login", response_model=TokenOut, tags=["Admin Auth"])
def admin_login(credentials: AdminLoginIn, db: Session = Depends(get_db)) -> TokenOut:
    admin = db.scalar(select(AdminCredential).where(AdminCredential.username == credentials.username))
    if admin is None or not verify_password(credentials.password, admin.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(access_token=create_access_token(admin.username))


# ── Messages ─────────────────────────────────────────────────────────────────

@api.get("/api/messages")
async def get_messages(
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> list[dict]:
    messages = db.scalars(select(Message).order_by(Message.created_at.asc())).all()
    return [serialize_message(message) for message in messages]


@api.post("/api/messages/send")
async def send_message(
    payload: SendMessageIn,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> dict:
    normalized_phone = re.sub(r"\D", "", payload.phone)
    if not normalized_phone:
        raise HTTPException(status_code=400, detail="Phone number is required")

    message = Message(phone=payload.phone, direction="outbound", content=payload.message)
    db.add(message)
    db.commit()
    db.refresh(message)
    response = serialize_message(message)
    await sio.emit("message:new", response)
    await send_whatsapp_text(normalized_phone, payload.message)
    return response


@api.post("/api/whatsapp/start")
async def start_whatsapp_template(
    payload: StartWhatsAppTemplateIn,
) -> dict:
    normalized_phone = re.sub(r"\D", "", payload.phone)
    if not normalized_phone:
        raise HTTPException(status_code=400, detail="Phone number is required")
    result = await send_whatsapp_template(normalized_phone)
    return {"success": True, "phone": normalized_phone, "provider_response": result}


# ── Uploads ───────────────────────────────────────────────────────────────────

@api.post("/api/uploads/products/image")
async def upload_product_image(
    file: UploadFile = File(...),
    _: AdminCredential = Depends(get_current_admin),
) -> dict[str, str]:
    suffix = Path(file.filename or "upload").suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type not allowed. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Image must be 5 MB or smaller",
        )
    if not _check_image_magic(content):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content does not match an allowed image format",
        )

    filename = f"{uuid4().hex}{suffix}"
    destination = PRODUCT_UPLOADS_DIR / filename
    destination.write_bytes(content)
    return {"image_url": f"/uploads/products/{filename}"}


# ── Categories ────────────────────────────────────────────────────────────────

@api.get("/api/categories")
async def get_categories(
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> list[dict]:
    categories = db.scalars(select(Category).order_by(Category.id.asc())).all()
    return [CategoryOut.model_validate(c).model_dump(mode="json") for c in categories]


@api.post("/api/categories")
async def create_category(
    payload: CreateCategoryIn,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> dict:
    category = Category(
        name=payload.name,
        description=payload.description,
        parent_id=payload.parent_id,
        status=payload.status,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    return CategoryOut.model_validate(category).model_dump(mode="json")


@api.put("/api/categories/{category_id}")
async def update_category(
    category_id: int,
    payload: CreateCategoryIn,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> dict:
    category = db.get(Category, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    category.name = payload.name
    category.description = payload.description
    category.parent_id = payload.parent_id
    category.status = payload.status
    db.commit()
    db.refresh(category)
    return CategoryOut.model_validate(category).model_dump(mode="json")


@api.delete("/api/categories/{category_id}")
async def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
):
    category = db.get(Category, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    products_count = db.scalar(
        select(func.count(Product.id)).where(Product.category_id == category_id)
    )
    if products_count and products_count > 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a category that has products assigned to it",
        )
    db.delete(category)
    db.commit()
    return {"success": True}


# ── Products ──────────────────────────────────────────────────────────────────

@api.get("/api/store/products")
async def get_store_products(
    db: Session = Depends(get_db),
) -> list[dict]:
    products = db.scalars(
        select(Product)
        .where(Product.status == "Active", Product.stock > 0)
        .order_by(Product.id.asc())
    ).all()
    return [serialize_product(product) for product in products]


@api.get("/api/products")
async def get_products(
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> list[dict]:
    products = db.scalars(select(Product).order_by(Product.id.asc())).all()
    return [serialize_product(product) for product in products]


@api.post("/api/products")
async def create_product(
    payload: CreateProductIn,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> dict:
    product = Product(
        name=payload.name,
        sku=payload.sku,
        barcode=payload.barcode,
        category_id=payload.category_id,
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
async def update_product(
    product_id: int,
    payload: CreateProductIn,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    product.name = payload.name
    product.sku = payload.sku
    product.barcode = payload.barcode
    product.category_id = payload.category_id
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


# ── Users ─────────────────────────────────────────────────────────────────────

@api.get("/api/users")
async def get_users(
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> list[dict]:
    users = db.scalars(select(User).order_by(User.id.asc())).all()
    return [UserOut.model_validate(user).model_dump(mode="json") for user in users]


# ── Orders (admin) ────────────────────────────────────────────────────────────

@api.get("/api/orders")
async def get_orders(
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> list[dict]:
    orders = db.scalars(
        select(Order)
        .order_by(Order.created_at.desc())
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.rejections),
        )
    ).all()
    return [serialize_order(order) for order in orders]


@api.patch("/api/orders/{order_id}/status")
async def update_order_status(
    order_id: int,
    payload: UpdateOrderStatusIn,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> dict:
    order = get_order_or_404(db, order_id)
    order.status = payload.status
    if payload.status == "Delivered":
        order.offered_to_delivery_boy_id = None
        order.offered_distance_km = None
    db.commit()
    order = get_order_or_404(db, order_id)
    response = serialize_order(order)
    await sio.emit("order:updated", response)
    return response


@api.post("/api/orders/{order_id}/assign")
async def assign_order(
    order_id: int,
    payload: AssignOrderIn,
    db: Session = Depends(get_db),
    _: AdminCredential = Depends(get_current_admin),
) -> dict:
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


# ── Orders (delivery) ─────────────────────────────────────────────────────────
# Note: these endpoints use delivery_boy_id in the URL as identity.
# TODO: implement delivery-boy auth tokens for production hardening.

@api.get("/api/delivery/{delivery_boy_id}/dashboard")
async def get_delivery_dashboard(
    delivery_boy_id: int,
    db: Session = Depends(get_db),
) -> dict:
    rider = db.get(User, delivery_boy_id)
    if rider is None or rider.role != "delivery":
        raise HTTPException(status_code=404, detail="Delivery user not found")

    dispatch_pending_orders(db)

    incoming_orders = db.scalars(
        select(Order)
        .where(Order.status == "Pending", Order.offered_to_delivery_boy_id == delivery_boy_id)
        .order_by(Order.created_at.asc())
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.rejections),
        )
    ).all()
    active_order = db.scalar(
        select(Order)
        .where(Order.status == "Assigned", Order.delivery_boy_id == delivery_boy_id)
        .order_by(Order.created_at.asc())
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.rejections),
        )
    )
    recent_orders = db.scalars(
        select(Order)
        .where(Order.status == "Delivered", Order.delivery_boy_id == delivery_boy_id)
        .order_by(Order.created_at.desc())
        .limit(10)
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.rejections),
        )
    ).all()

    return DeliveryDashboardOut(
        incoming_orders=[OrderOut.model_validate(serialize_order(o)) for o in incoming_orders],
        active_order=OrderOut.model_validate(serialize_order(active_order)) if active_order else None,
        recent_orders=[OrderOut.model_validate(serialize_order(o)) for o in recent_orders],
    ).model_dump(mode="json")


@api.post("/api/orders/{order_id}/accept")
async def accept_order_offer(
    order_id: int,
    payload: DeliveryDecisionIn,
    db: Session = Depends(get_db),
) -> dict:
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
async def reject_order_offer(
    order_id: int,
    payload: DeliveryDecisionIn,
    db: Session = Depends(get_db),
) -> dict:
    order = get_order_or_404(db, order_id)
    if order.status != "Pending":
        raise HTTPException(status_code=409, detail="Order is no longer pending")
    if order.offered_to_delivery_boy_id != payload.delivery_boy_id:
        raise HTTPException(status_code=409, detail="Order is not currently offered to this rider")

    already_rejected = any(
        r.delivery_boy_id == payload.delivery_boy_id for r in order.rejections
    )
    if not already_rejected:
        db.add(OrderRejection(order_id=order.id, delivery_boy_id=payload.delivery_boy_id))

    order.offered_to_delivery_boy_id = None
    order.offered_distance_km = None
    db.commit()

    order = get_order_or_404(db, order_id)
    dispatch_order_to_best_rider(db, order)
    order = get_order_or_404(db, order_id)
    response = serialize_order(order)
    await sio.emit("order:updated", response)
    return response


# ── WhatsApp Webhook ──────────────────────────────────────────────────────────

async def process_whatsapp_message(phone: str, message_text: str, db: Session) -> dict:
    inbound_message = Message(phone=phone, direction="inbound", content=message_text)
    db.add(inbound_message)
    db.commit()
    db.refresh(inbound_message)
    await sio.emit("message:new", serialize_message(inbound_message))

    normalized_phone = re.sub(r"\D", "", phone)

    if message_text.strip().lower() in ["/show items", "view product", "view_product"]:
        reply_text = build_product_catalog_message(db)
        outbound_message = Message(phone=phone, direction="outbound", content=reply_text)
        db.add(outbound_message)
        db.commit()
        db.refresh(outbound_message)
        await sio.emit("message:new", serialize_message(outbound_message))
        await send_whatsapp_text(normalized_phone, reply_text)
        return {"success": True, "message": "Product list sent"}

    parsed_items, parse_error = parse_order_message(db, message_text)
    if not parsed_items or parse_error:
        welcome_text = build_product_catalog_message(db)
        fallback = Message(phone=phone, direction="outbound", content=welcome_text)
        db.add(fallback)
        db.commit()
        db.refresh(fallback)
        await sio.emit("message:new", serialize_message(fallback))
        await send_whatsapp_text(normalized_phone, welcome_text)
        return {"success": True, "message": "Product list sent"}

    order = Order(
        customer_name=f"Customer {phone[-4:]}",
        customer_phone=phone,
        customer_address="",
        customer_latitude=0.0,
        customer_longitude=0.0,
        status="Pending",
    )
    db.add(order)
    db.flush()

    # Atomically deduct stock with a conditional UPDATE to prevent race conditions.
    # If another request already consumed the stock, rowcount will be 0 and we rollback.
    failed_product_name: str | None = None
    for product, quantity in parsed_items:
        result = db.execute(
            update(Product)
            .where(Product.id == product.id, Product.stock >= quantity)
            .values(stock=Product.stock - quantity)
            .returning(Product.id)
        )
        if result.fetchone() is None:
            failed_product_name = product.name
            break
        db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity))

    if failed_product_name:
        db.rollback()
        error_msg = f"Sorry, {failed_product_name} is out of stock or has insufficient quantity."
        outbound = Message(phone=phone, direction="outbound", content=error_msg)
        db.add(outbound)
        db.commit()
        await send_whatsapp_text(normalized_phone, error_msg)
        return {"success": False, "message": "Insufficient stock"}

    confirmation = Message(
        phone=phone,
        direction="outbound",
        content=build_whatsapp_reply(order.id),
    )
    db.add(confirmation)
    db.commit()
    db.refresh(confirmation)

    full_order = get_order_or_404(db, order.id)
    # Auto-dispatch skipped: customer location is unknown from WhatsApp text.
    # Admin can manually assign via POST /api/orders/{id}/assign.
    order_payload = serialize_order(full_order)
    await sio.emit("order:created", order_payload)
    await sio.emit("inventory:updated")
    await sio.emit("message:new", serialize_message(confirmation))
    await send_whatsapp_text(normalized_phone, confirmation.content)

    return {"success": True, "order": order_payload, "message": "Order placed successfully!"}


@api.get("/api/webhook/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    configured_token = settings.whatsapp_verify_token
    if not configured_token:
        raise HTTPException(
            status_code=503,
            detail="Webhook verify token is not configured. Set WHATSAPP_VERIFY_TOKEN in .env.",
        )
    if hub_mode == "subscribe" and hub_challenge:
        if hub_verify_token != configured_token:
            raise HTTPException(status_code=403, detail="Invalid verify token")
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Forbidden")


@api.post("/api/webhook/whatsapp")
async def whatsapp_webhook(request: FastAPIRequest, db: Session = Depends(get_db)):
    raw_body = await request.body()

    app_secret = settings.whatsapp_app_secret
    if app_secret:
        signature_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_header):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if payload.get("object") == "whatsapp_business_account":
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        phone = msg.get("from")
                        message_text = ""
                        if msg.get("type") == "text":
                            message_text = msg["text"]["body"]
                        elif msg.get("type") == "interactive":
                            message_text = msg["interactive"]["button_reply"]["title"]
                        else:
                            continue
                        if phone and message_text:
                            display_phone = phone if phone.startswith("+") else f"+{phone}"
                            await process_whatsapp_message(display_phone, message_text, db)
        return Response(content="ok", media_type="text/plain")
    else:
        phone = payload.get("phone")
        message_text = payload.get("message")
        if phone and message_text:
            return await process_whatsapp_message(phone, message_text, db)
        return {"success": False, "message": "Unknown payload structure"}


app = socketio.ASGIApp(socketio_server=sio, other_asgi_app=api, socketio_path="socket.io")

