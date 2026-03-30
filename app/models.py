from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    base_latitude: Mapped[float] = mapped_column(Float, nullable=False, default=0, server_default="0")
    base_longitude: Mapped[float] = mapped_column(Float, nullable=False, default=0, server_default="0")
    service_radius_km: Mapped[float] = mapped_column(Float, nullable=False, default=5, server_default="5")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="Active", server_default="Active")

    parent: Mapped["Category"] = relationship(remote_side="Category.id")
    products: Mapped[list["Product"]] = relationship(back_populates="category_obj")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    barcode: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    brand: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    unit: Mapped[str] = mapped_column(String(50), nullable=False, default="unit", server_default="unit")
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    cost_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0, server_default="0")
    mrp: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0, server_default="0")
    stock: Mapped[int] = mapped_column(nullable=False)
    reorder_level: Mapped[int] = mapped_column(nullable=False, default=10, server_default="10")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="Active", server_default="Active")
    location: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    supplier_name: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    supplier_phone: Mapped[str] = mapped_column(String(50), nullable=False, default="", server_default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    image_url: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    category_obj: Mapped["Category | None"] = relationship(back_populates="products", lazy="joined")

    @property
    def category_name(self) -> str:
        return self.category_obj.name if self.category_obj else ""


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False, default="Customer", server_default="Customer")
    customer_phone: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_address: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    customer_latitude: Mapped[float] = mapped_column(Float, nullable=False, default=0, server_default="0")
    customer_longitude: Mapped[float] = mapped_column(Float, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="Pending", server_default="Pending")
    delivery_boy_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    offered_to_delivery_boy_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    offered_distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    rejections: Mapped[list["OrderRejection"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class OrderRejection(Base):
    __tablename__ = "order_rejections"
    __table_args__ = (UniqueConstraint("order_id", "delivery_boy_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    delivery_boy_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    order: Mapped[Order] = relationship(back_populates="rejections")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(lazy="joined")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class AdminCredential(Base):
    __tablename__ = "admin_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
