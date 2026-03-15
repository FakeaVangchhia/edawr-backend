from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageOut(ORMModel):
    id: int
    phone: str
    direction: Literal["inbound", "outbound"]
    content: str
    created_at: datetime


class ProductOut(ORMModel):
    id: int
    name: str
    sku: str
    category: str
    brand: str
    unit: str
    price: float
    cost_price: float
    mrp: float
    stock: int
    reorder_level: int
    status: str
    location: str
    supplier_name: str
    supplier_phone: str
    description: str
    image_url: str


class UserOut(ORMModel):
    id: int
    name: str
    role: Literal["manager", "delivery"]
    phone: str
    base_latitude: float
    base_longitude: float
    service_radius_km: float


class OrderItemOut(BaseModel):
    id: int
    order_id: int
    product_id: int
    quantity: int
    name: str
    price: float


class OrderOut(BaseModel):
    id: int
    customer_name: str
    customer_phone: str
    customer_address: str
    customer_latitude: float
    customer_longitude: float
    status: Literal["Pending", "Assigned", "Delivered"]
    delivery_boy_id: int | None
    offered_to_delivery_boy_id: int | None
    offered_distance_km: float | None
    created_at: datetime
    items: list[OrderItemOut]


class DeliveryDashboardOut(BaseModel):
    incoming_orders: list[OrderOut]
    active_order: OrderOut | None
    recent_orders: list[OrderOut]


class SendMessageIn(BaseModel):
    phone: str
    message: str


class CreateProductIn(BaseModel):
    name: str
    sku: str = ""
    category: str = "General"
    brand: str = ""
    unit: str = "unit"
    price: float
    cost_price: float = 0
    mrp: float = 0
    stock: int
    reorder_level: int = 10
    status: str = "Active"
    location: str = ""
    supplier_name: str = ""
    supplier_phone: str = ""
    description: str = ""
    image_url: str = ""


class UpdateOrderStatusIn(BaseModel):
    status: Literal["Pending", "Assigned", "Delivered"]


class AssignOrderIn(BaseModel):
    delivery_boy_id: int


class DeliveryDecisionIn(BaseModel):
    delivery_boy_id: int


class WhatsAppWebhookIn(BaseModel):
    phone: str
    message: str
