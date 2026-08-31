# Task 2.6/2.8 — shared pytest fixtures.
#
# DB_NAME override MUST happen before any `app.*` module is imported
# anywhere (including transitively) — app.core.config builds a cached
# Settings singleton and app.db.mongo opens its Motor client from it at
# import time. Runs against a real, separate database on the same Atlas
# cluster (not mongomock) so tests exercise real Beanie/Motor behavior;
# the whole database is dropped after the session so nothing lingers.
import os

os.environ["DB_NAME"] = "voiceagent_test"

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Motor's AsyncIOMotorClient (built once, at app.db.mongo import time) is
# bound to whatever event loop exists when it's constructed. pytest-asyncio
# defaults to a fresh loop per test function, which then hits the client
# from a different loop than it was created on ("Future attached to a
# different loop"). Pinning every async fixture/test to one session-scoped
# loop keeps it all on the loop the client was actually built on.
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _test_db():
    from app.db.mongo import database
    from app.models.appointment import Appointment
    from app.models.bot import Bot
    from app.models.conversation import ConversationTurn
    from app.models.document import Document
    from app.models.order import Order
    from app.models.user import User
    from app.db.mongo import init_db

    await init_db([User, Bot, Document, Order, Appointment, ConversationTurn])
    yield
    await database.client.drop_database(database.name)


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    from main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register_and_login(client: AsyncClient, email: str, password: str = "testpass123") -> str:
    await client.post("/auth/register", json={"email": email, "password": password})
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


@pytest_asyncio.fixture(loop_scope="session")
async def user_a_token(client):
    return await _register_and_login(client, "user-a@voiceagent-test.com")


@pytest_asyncio.fixture(loop_scope="session")
async def user_b_token(client):
    return await _register_and_login(client, "user-b@voiceagent-test.com")


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
