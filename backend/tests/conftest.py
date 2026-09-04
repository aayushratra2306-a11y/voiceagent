# Task 2.6/2.8 — shared pytest fixtures.
#
# DB_NAME override MUST happen before any `app.*` module is imported
# anywhere (including transitively) — app.core.config builds a cached
# Settings singleton and app.db.mongo opens its Motor client from it at
# import time. Runs against a real, separate database on the same Atlas
# cluster (not mongomock) so tests exercise real Beanie/Motor behavior;
# the whole database is dropped after the session so nothing lingers.
#
# setdefault, not a hard assignment: a local run and a CI run hitting the
# same "voiceagent_test" database concurrently would drop each other's data
# mid-test (the session teardown drops the whole database). CI sets its own
# DB_NAME (see .github/workflows/ci.yml) specifically to avoid that; this is
# just the fallback for running pytest locally with no override.
import os

os.environ.setdefault("DB_NAME", "voiceagent_test")

# Task 2.7 — deliberately blanked, not setdefault'd: once a real DSN exists
# in backend/.env, every deliberately-triggered error in this suite (the
# rate-limit test, the ownership 404s) would otherwise be reported to the
# production Sentry project as if it were a real incident. A blank DSN makes
# sentry_sdk.init() a confirmed no-op. Must happen before main.py is
# imported, which is where init() runs.
os.environ["SENTRY_DSN"] = ""

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
    from app.db.mongo import database, init_db
    from app.models.appointment import Appointment
    from app.models.bot import Bot
    from app.models.bot_tool import BotTool
    from app.models.conversation import ConversationTurn
    from app.models.document import Document
    from app.models.order import Order
    from app.models.revoked_token import RevokedRefreshToken
    from app.models.user import User

    await init_db([User, Bot, Document, Order, Appointment, ConversationTurn, RevokedRefreshToken, BotTool])
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


# Task 2.5 added a 5/minute rate limit to /auth/login and /auth/register —
# real and correct in production, but this test file alone would otherwise
# call login() once per test function (each needing a fresh access token),
# which blows through that limit in seconds and starts failing tests with
# 429s that have nothing to do with what's actually being tested. Logging
# in once per test SESSION (cached here) instead of once per TEST is also
# just better test design regardless of the rate limit — there's no reason
# to re-authenticate for every single test.
_token_cache: dict[str, str] = {}


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _session_client():
    from main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def user_a_token(_session_client):
    if "a" not in _token_cache:
        _token_cache["a"] = await _register_and_login(_session_client, "user-a@voiceagent-test.com")
    return _token_cache["a"]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def user_b_token(_session_client):
    if "b" not in _token_cache:
        _token_cache["b"] = await _register_and_login(_session_client, "user-b@voiceagent-test.com")
    return _token_cache["b"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
