from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_password
from .models import AdminCredential, Category, Order, OrderItem, Product, User


def _get_or_create_category(db: Session, name: str, description: str = "") -> Category:
    category = db.scalar(select(Category).where(Category.name == name))
    if category is None:
        category = Category(name=name, description=description, status="Active")
        db.add(category)
        db.flush()
    return category


def seed_admin_credentials(db: Session) -> None:
    from .config import get_settings
    settings = get_settings()
    exists = db.scalar(select(AdminCredential).limit(1))
    if exists is None:
        db.add(AdminCredential(
            username=settings.admin_default_username,
            hashed_password=hash_password(settings.admin_default_password),
        ))
        db.commit()


def seed_initial_data(db: Session) -> None:
    dairy = _get_or_create_category(db, "Dairy", "Dairy products")
    bakery = _get_or_create_category(db, "Bakery", "Baked goods")
    _get_or_create_category(db, "Beverages", "Drinks and beverages")
    _get_or_create_category(db, "General", "General items")
    db.commit()

    has_users = db.scalar(select(User.id).limit(1))
    if has_users is None:
        db.add_all([
            User(name="Admin", role="manager", phone="1234567890"),
            User(
                name="John Delivery",
                role="delivery",
                phone="0987654321",
                base_latitude=12.9716,
                base_longitude=77.5946,
                service_radius_km=5,
            ),
            User(
                name="Mike Delivery",
                role="delivery",
                phone="1112223333",
                base_latitude=12.9352,
                base_longitude=77.6245,
                service_radius_km=4,
            ),
            Product(
                name="Milk",
                sku="DAI-MIL-001",
                category_id=dairy.id,
                brand="Farm Fresh",
                unit="1 L",
                price=2.50,
                cost_price=1.80,
                mrp=2.80,
                stock=100,
                reorder_level=30,
                status="Active",
                location="Cold Rack A1",
                supplier_name="Sunrise Dairy",
                supplier_phone="9000000001",
                description="Pasteurized full cream milk.",
            ),
            Product(
                name="Bread",
                sku="BAK-BRE-014",
                category_id=bakery.id,
                brand="Daily Bake",
                unit="400 g",
                price=1.50,
                cost_price=1.00,
                mrp=1.80,
                stock=50,
                reorder_level=20,
                status="Active",
                location="Bakery Shelf B2",
                supplier_name="Daily Bake Foods",
                supplier_phone="9000000002",
                description="Soft sandwich bread loaf.",
            ),
            Product(
                name="Eggs",
                sku="DAI-EGG-021",
                category_id=dairy.id,
                brand="Happy Hen",
                unit="12 pcs",
                price=3.00,
                cost_price=2.25,
                mrp=3.40,
                stock=200,
                reorder_level=60,
                status="Active",
                location="Cold Rack A4",
                supplier_name="Happy Hen Farms",
                supplier_phone="9000000003",
                description="Grade A farm eggs.",
            ),
        ])
        db.commit()

    riders = db.scalars(select(User).where(User.role == "delivery").order_by(User.id.asc())).all()
    coordinate_fallbacks = [
        (12.9716, 77.5946, 5),
        (12.9352, 77.6245, 4),
        (12.9980, 77.6387, 6),
    ]
    for index, rider in enumerate(riders):
        if rider.base_latitude == 0 and rider.base_longitude == 0:
            lat, lng, radius = coordinate_fallbacks[min(index, len(coordinate_fallbacks) - 1)]
            rider.base_latitude = lat
            rider.base_longitude = lng
            rider.service_radius_km = radius
    db.commit()

    has_orders = db.scalar(select(Order.id).limit(1))
    if has_orders is not None:
        return

    products = db.scalars(select(Product).order_by(Product.id.asc())).all()
    if len(products) < 3:
        return

    sample_orders = [
        Order(
            customer_name="Asha Nair",
            customer_phone="9001002001",
            customer_address="Richmond Road, Bengaluru",
            customer_latitude=12.9647,
            customer_longitude=77.6092,
            status="Pending",
        ),
        Order(
            customer_name="Rahul Verma",
            customer_phone="9001002002",
            customer_address="Koramangala 5th Block, Bengaluru",
            customer_latitude=12.9349,
            customer_longitude=77.6207,
            status="Pending",
        ),
        Order(
            customer_name="Meera Iyer",
            customer_phone="9001002003",
            customer_address="Indiranagar 100 Feet Road, Bengaluru",
            customer_latitude=12.9784,
            customer_longitude=77.6408,
            status="Pending",
        ),
    ]
    db.add_all(sample_orders)
    db.flush()

    db.add_all([
        OrderItem(order_id=sample_orders[0].id, product_id=products[0].id, quantity=2),
        OrderItem(order_id=sample_orders[0].id, product_id=products[1].id, quantity=1),
        OrderItem(order_id=sample_orders[1].id, product_id=products[2].id, quantity=1),
        OrderItem(order_id=sample_orders[1].id, product_id=products[0].id, quantity=1),
        OrderItem(order_id=sample_orders[2].id, product_id=products[1].id, quantity=2),
    ])
    db.commit()
