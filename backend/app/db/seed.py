from loguru import logger

from app.models.order import Order

# Deliberately does NOT include an order 9999 or similar — that gap is what
# lets you test the "order not found" case naturally, per the manual's own
# tip: a bot that says "I couldn't find that order" is production quality;
# one that invents a delivery date for a nonexistent order is dangerous.
_FAKE_ORDERS = [
    {"order_id": "ORD-1001", "item_name": "wireless headphones", "status": "shipped", "delivery_date": "3rd September", "customer_name": "Aayush Ratra"},
    {"order_id": "ORD-1002", "item_name": "running shoes", "status": "delivered", "delivery_date": "28th August", "customer_name": "Priya Sharma"},
    {"order_id": "ORD-1003", "item_name": "laptop stand", "status": "processing", "delivery_date": "10th September", "customer_name": "Rahul Verma"},
]


async def seed_fake_orders():
    """Insert demo orders once, on first startup only — never touches the
    collection again if it's already populated (so nothing gets overwritten
    if you add real test data of your own later)."""
    if await Order.find_one({}):
        return
    await Order.insert_many([Order(**o) for o in _FAKE_ORDERS])
    logger.info(f"[SEED] Inserted {len(_FAKE_ORDERS)} fake orders for testing (ORD-1001..1003)")
