from beanie import Document


class Order(Document):
    """Fake order data for Task 1.4's lookup tool demo — not a real orders
    system, just realistic-enough data to prove the tool mechanism end to end."""

    order_id: str  # human-friendly, e.g. "ORD-1001" — matches how a caller would say it
    item_name: str
    status: str  # "processing" | "shipped" | "delivered"
    delivery_date: str  # human-readable, e.g. "3rd September"
    customer_name: str

    class Settings:
        name = "orders"
