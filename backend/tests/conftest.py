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

import pytest

from app.core.db_safety import is_disposable_database

# 2026-09-13 — this line used to be the whole guard, and it dropped
# production. setdefault only fills a MISSING value; the live container
# already had DB_NAME=voiceagent, so the suite kept it, and the teardown
# below deleted every account, bot and document on the server. Now the name
# has to declare itself disposable, or nothing runs — see
# tests/test_database_safety.py.
os.environ.setdefault("DB_NAME", "voiceagent_test")
if not is_disposable_database(os.environ["DB_NAME"]):
    pytest.exit(
        f"Refusing to run the test suite against database {os.environ['DB_NAME']!r}. "
        f"The suite DROPS its database when it finishes, so DB_NAME must end in "
        f"'_test' or '_ci'. Never run the tests on the production server.",
        returncode=3,
    )

# Task 2.7 — deliberately blanked, not setdefault'd: once a real DSN exists
# in backend/.env, every deliberately-triggered error in this suite (the
# rate-limit test, the ownership 404s) would otherwise be reported to the
# production Sentry project as if it were a real incident. A blank DSN makes
# sentry_sdk.init() a confirmed no-op. Must happen before main.py is
# imported, which is where init() runs.
os.environ["SENTRY_DSN"] = ""

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
    from app.models.registry import ALL_MODELS

    await init_db(ALL_MODELS)
    yield
    # Checked again at the moment it matters, not only at import: the name
    # that reaches this line is the one actually connected to, and it is the
    # one that gets destroyed.
    if not is_disposable_database(database.name):
        raise RuntimeError(f"Refusing to drop non-disposable database {database.name!r}")
    await database.client.drop_database(database.name)


@pytest.fixture(autouse=True)
def _offline_resolver(monkeypatch):
    """Name resolution, made deterministic and offline for the whole suite.

    Review finding I4 (2026-09-10) made app/core/url_safety.py fail CLOSED:
    a host name that cannot be resolved is now a rejection rather than a
    pass, because the person who chose the URL also owns the nameserver
    that answers for it, and "make the check unable to run" was the way
    past it.

    Almost every tool and webhook test uses a deliberately fake host —
    api.example.com, ok.test, slow.test — which of course does not
    resolve, so under the new rule the suite would be testing the SSRF
    guard's refusal path over and over instead of the behaviour each test
    is actually about. Worse, the ones that DID resolve were reaching the
    real DNS from a unit test, which is slow, flaky offline, and dependent
    on whatever the network happens to think today.

    So: one fake resolver for the whole suite, and no test touches DNS.

      - `localhost` answers with 127.0.0.1, because tests that assert an
        internal address is refused name it and must keep getting refused;
      - anything under `.invalid` fails, which is what that RFC-reserved
        suffix is for and is how the fail-closed path is exercised;
      - everything else answers with one ordinary public address.

    Literal addresses never reach here — url_safety judges those directly.
    Tests that need a specific answer (a name that resolves differently
    the second time, say) monkeypatch over this; see test_ssrf_pinning.py.
    """
    from app.core import url_safety

    def fake_resolve(host: str) -> list[str]:
        if host == "localhost":
            return ["127.0.0.1"]
        if host.endswith(".invalid"):
            raise OSError(f"deliberately unresolvable in tests: {host}")
        return ["93.184.216.34"]

    monkeypatch.setattr(url_safety, "_resolve_addresses", fake_resolve)


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    from main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Review finding I6 (2026-09-10) raised the registration floor to 12
# characters, and this default was 11 ("testpass123") — every fixture
# built on it would have started failing validation rather than testing
# what it is about.
#
# Task 5.1 — every token this hands out is remembered with its owner's
# personal organisation (in _org_of_token, added in Task 2), so
# auth_headers(token) keeps meaning "this user, in their own workspace".
# Existing cross-user tests thereby become cross-ORGANISATION tests without
# being rewritten.
async def _register_and_login(client: AsyncClient, email: str, password: str = "testpass12345") -> str:
    await client.post("/auth/register", json={"email": email, "password": password})
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    token = resp.json()["access_token"]
    orgs = await client.get("/orgs", headers={"Authorization": f"Bearer {token}"})
    personal = [o for o in orgs.json() if o["personal"]]
    if personal:
        _org_of_token[token] = personal[0]["id"]
    return token


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


# Task 5.1 — token -> that user's personal organisation, so auth_headers(token)
# can mean "this user, in their own workspace" (see Task 5).
_org_of_token: dict[str, str] = {}


def auth_headers(token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if token in _org_of_token:
        headers["X-Org-Id"] = _org_of_token[token]
    return headers


def org_headers(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


async def make_user(email: str) -> str:
    """A user, their personal organisation and an access token, without
    going through the rate-limited /auth routes. Returns the token."""
    from app.core.auth import create_access_token
    from app.models.user import User
    from app.services.orgs import ensure_personal_org

    user = await User.find_one(User.email == email)
    if user is None:
        user = User(email=email, password_hash="not-used-by-this-helper")
        await user.insert()
    org = await ensure_personal_org(user)
    token = create_access_token({"sub": email})
    if org is not None:
        _org_of_token[token] = str(org.id)
    else:
        from app.models.organisation import Membership

        m = await Membership.find_one(Membership.user_id == str(user.id))
        _org_of_token[token] = m.org_id
    return token
