import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.org import OrgContext, require_role
from app.models.organisation import Membership, Organisation
from tests.conftest import make_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

probe = FastAPI()


@probe.get("/member-thing")
async def member_thing(ctx: OrgContext = Depends(require_role("member"))):
    return {"org": ctx.org_id, "role": ctx.role}


@probe.get("/orgs/{org_id}/admin-thing")
async def admin_thing(ctx: OrgContext = Depends(require_role("admin", from_path=True))):
    return {"org": ctx.org_id}


async def _probe_client():
    return AsyncClient(transport=ASGITransport(app=probe), base_url="http://probe")


async def _org_with(user_email: str, role: str, client) -> tuple[str, str]:
    token = await make_user(user_email)
    from app.models.user import User
    user = await User.find_one(User.email == user_email)
    org = Organisation(name="Probe org", created_by="someone")
    await org.insert()
    await Membership(org_id=str(org.id), user_id=str(user.id), role=role).insert()
    return token, str(org.id)


async def test_no_token_is_401_before_any_org_check():
    async with await _probe_client() as c:
        assert (await c.get("/member-thing")).status_code in (401, 403)  # HTTPBearer: 403 when absent


async def test_missing_org_header_is_400_not_422(client):
    token, _ = await _org_with("ctx-1@voiceagent-test.com", "member", client)
    async with await _probe_client() as c:
        r = await c.get("/member-thing", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400 and r.json()["detail"] == "Choose an organisation"


async def test_malformed_org_id_is_404(client):
    token, _ = await _org_with("ctx-2@voiceagent-test.com", "member", client)
    async with await _probe_client() as c:
        r = await c.get("/member-thing", headers={"Authorization": f"Bearer {token}", "X-Org-Id": "nope"})
    assert r.status_code == 404


async def test_non_member_is_404(client):
    token, _ = await _org_with("ctx-3@voiceagent-test.com", "member", client)
    stranger = Organisation(name="Not yours", created_by="x")
    await stranger.insert()
    async with await _probe_client() as c:
        r = await c.get("/member-thing", headers={"Authorization": f"Bearer {token}", "X-Org-Id": str(stranger.id)})
    assert r.status_code == 404


async def test_low_role_is_403_with_its_name(client):
    token, org_id = await _org_with("ctx-4@voiceagent-test.com", "viewer", client)
    async with await _probe_client() as c:
        r = await c.get("/member-thing", headers={"Authorization": f"Bearer {token}", "X-Org-Id": org_id})
    assert r.status_code == 403 and "viewer" in r.json()["detail"]


async def test_enough_role_passes(client):
    token, org_id = await _org_with("ctx-5@voiceagent-test.com", "admin", client)
    async with await _probe_client() as c:
        r = await c.get("/member-thing", headers={"Authorization": f"Bearer {token}", "X-Org-Id": org_id})
    assert r.status_code == 200 and r.json() == {"org": org_id, "role": "admin"}


async def test_path_org_wins_and_a_different_header_is_404(client):
    """Admin of A cannot act on /orgs/B by sending X-Org-Id: A (spec C1)."""
    token, org_a = await _org_with("ctx-6@voiceagent-test.com", "admin", client)
    org_b = Organisation(name="B", created_by="x")
    await org_b.insert()
    h = {"Authorization": f"Bearer {token}"}
    async with await _probe_client() as c:
        assert (await c.get(f"/orgs/{org_a}/admin-thing", headers=h)).status_code == 200
        assert (await c.get(f"/orgs/{org_b.id}/admin-thing", headers={**h, "X-Org-Id": org_a})).status_code == 404
        assert (await c.get(f"/orgs/{org_a}/admin-thing", headers={**h, "X-Org-Id": str(org_b.id)})).status_code == 404
