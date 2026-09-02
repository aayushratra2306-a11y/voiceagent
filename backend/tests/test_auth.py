# Task 2.5 — proves the refresh/revocation/rate-limit machinery actually
# works, not just that the code compiles. Uses its own throwaway users and
# its own AsyncClient instances (not the shared session-cached ones in
# conftest.py) since these tests specifically need to exercise login/
# refresh/logout/rate-limiting themselves, not just carry a token.
import itertools

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="session")

# The rate-limit key function falls back to per-IP for unauthenticated
# requests (see app/core/rate_limit.py), and ASGITransport gives every
# client the same fake ('127.0.0.1', 123) address unless told otherwise —
# so every test in this file would otherwise share ONE rate-limit bucket,
# and test_login_is_rate_limited deliberately exhausting it would spuriously
# 429 every other test's login calls too. Each client gets its own fake IP
# instead, isolating their rate-limit buckets from each other.
_next_fake_ip = itertools.count(1)


async def _fresh_client() -> AsyncClient:
    from main import app

    # https, not http: the refresh cookie is Secure-flagged (see auth.py),
    # and unlike a real browser (which exempts "localhost" specifically),
    # httpx's cookie jar has no such exception and will silently refuse to
    # resend a Secure cookie over a plain http:// base_url — confirmed live
    # while writing this suite (every refresh-cookie test below failed with
    # a spurious 401 until this was https). Real browsers on
    # http://localhost are unaffected; this is purely a test-harness detail.
    fake_ip = f"10.0.0.{next(_next_fake_ip)}"
    transport = ASGITransport(app=app, client=(fake_ip, 12345))
    return AsyncClient(transport=transport, base_url="https://test")


async def test_login_sets_httponly_refresh_cookie():
    async with await _fresh_client() as client:
        await client.post("/auth/register", json={"email": "refresh-a@voiceagent-test.com", "password": "pw12345"})
        resp = await client.post("/auth/login", json={"email": "refresh-a@voiceagent-test.com", "password": "pw12345"})
        assert resp.status_code == 200
        assert "access_token" in resp.json()
        assert "refresh_token" in client.cookies
        # httpx's cookie jar doesn't expose httponly/secure flags directly,
        # but Set-Cookie is right there in the response headers — assert on
        # the actual wire format rather than trusting client.cookies alone.
        set_cookie = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie or "SameSite=Lax" in set_cookie


async def test_refresh_issues_a_new_access_token():
    async with await _fresh_client() as client:
        await client.post("/auth/register", json={"email": "refresh-b@voiceagent-test.com", "password": "pw12345"})
        await client.post("/auth/login", json={"email": "refresh-b@voiceagent-test.com", "password": "pw12345"})

        refresh_resp = await client.post("/auth/refresh")
        assert refresh_resp.status_code == 200
        new_access_token = refresh_resp.json()["access_token"]
        # Not asserting new_access_token != original_access_token: a JWT's
        # bytes are a deterministic function of its claims, and two tokens
        # minted for the same user within the same second (entirely
        # possible for login immediately followed by refresh, as here)
        # legitimately encode identically — that's not a bug. What matters
        # is that refresh genuinely issues a *working* token, checked below.
        me_check = await client.get("/bots/", headers={"Authorization": f"Bearer {new_access_token}"})
        assert me_check.status_code == 200


async def test_refresh_token_is_rotated_and_old_one_stops_working():
    async with await _fresh_client() as client:
        await client.post("/auth/register", json={"email": "refresh-c@voiceagent-test.com", "password": "pw12345"})
        await client.post("/auth/login", json={"email": "refresh-c@voiceagent-test.com", "password": "pw12345"})
        old_refresh_cookie = client.cookies["refresh_token"]

        await client.post("/auth/refresh")  # rotates: old jti revoked, new cookie issued
        assert client.cookies["refresh_token"] != old_refresh_cookie

        # Manually replay the OLD refresh cookie (simulating a stolen copy
        # used after the legitimate client already rotated past it).
        client.cookies.set("refresh_token", old_refresh_cookie)
        replay_resp = await client.post("/auth/refresh")
        assert replay_resp.status_code == 401


async def test_logout_revokes_the_refresh_token():
    async with await _fresh_client() as client:
        await client.post("/auth/register", json={"email": "logout-a@voiceagent-test.com", "password": "pw12345"})
        await client.post("/auth/login", json={"email": "logout-a@voiceagent-test.com", "password": "pw12345"})

        logout_resp = await client.post("/auth/logout")
        assert logout_resp.status_code == 200

        # The session is genuinely over — refreshing with the now-revoked
        # cookie must fail, not silently keep working.
        refresh_after_logout = await client.post("/auth/refresh")
        assert refresh_after_logout.status_code == 401


async def test_a_refresh_token_cannot_be_used_as_an_access_token():
    """A refresh token is just a JWT signed with the same key as an access
    token — without an explicit type check, a leaked refresh token would
    ALSO work as a (long-lived!) access token, defeating the entire point
    of separating them."""
    async with await _fresh_client() as client:
        await client.post("/auth/register", json={"email": "typeconfusion@voiceagent-test.com", "password": "pw12345"})
        await client.post("/auth/login", json={"email": "typeconfusion@voiceagent-test.com", "password": "pw12345"})
        refresh_token = client.cookies["refresh_token"]

        resp = await client.get("/bots/", headers={"Authorization": f"Bearer {refresh_token}"})
        assert resp.status_code == 401


async def test_login_is_rate_limited():
    async with await _fresh_client() as client:
        await client.post("/auth/register", json={"email": "ratelimit@voiceagent-test.com", "password": "pw12345"})
        # The limit is 5/minute; a wrong password still counts as a request
        # against it (rate limiting has to apply before credential
        # checking, or it protects nothing against a brute-force attempt).
        responses = [
            await client.post("/auth/login", json={"email": "ratelimit@voiceagent-test.com", "password": "wrong"})
            for _ in range(7)
        ]
        statuses = [r.status_code for r in responses]
        assert 429 in statuses, f"expected a 429 somewhere in 7 rapid attempts, got {statuses}"
