import motor.motor_asyncio
from beanie import init_beanie

from app.core.config import settings

client = motor.motor_asyncio.AsyncIOMotorClient(
    settings.mongodb_url,
    serverSelectionTimeoutMS=5000,
)
database = client[settings.db_name]


async def init_db(document_models: list):
    await init_beanie(database=database, document_models=document_models)
