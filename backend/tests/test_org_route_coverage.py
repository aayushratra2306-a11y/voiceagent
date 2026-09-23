"""No route that touches customer data ships without the organisation check.

FastAPI 0.141 (pinned in requirements.txt) resolves included routers
lazily: `app.routes` holds top-level routes plus opaque `_IncludedRouter`
wrappers for every `app.include_router(...)` call, so a plain
`isinstance(route, APIRoute)` filter over `app.routes` only ever sees the
four routes declared directly on `app` (/health, /health/detail, /metrics,
/test) and silently skips every bot/tool/document/webhook/approval/payment/
connect/org route — a coverage test that can never see a gap, let alone
fail on one. `fastapi.routing.iter_route_contexts` is FastAPI's own helper
(used internally to build the OpenAPI schema) for walking that structure
and handing back the real, underlying route for each included router, so
that's what this test walks instead.
"""

from fastapi.routing import APIRoute, iter_route_contexts

from app.core.org import get_org_context, get_org_context_from_path
from main import app

EXEMPT = {
    ("POST", "/auth/register"): "creates the login",
    ("POST", "/auth/login"): "creates a session",
    ("POST", "/auth/refresh"): "session only",
    ("POST", "/auth/logout"): "session only",
    ("GET", "/orgs"): "lists the caller's own memberships",
    ("POST", "/orgs"): "creates a new organisation for the caller",
    ("GET", "/bots/templates"): "static catalogue",
    ("GET", "/webhooks/events"): "static list of event names",
    ("GET", "/connect/ice-servers"): "per-user TURN credentials, no tenant data",
    ("POST", "/connect/ice"): "per-user check on the live call (a call belongs to a person)",
    ("POST", "/payments/webhook/{tool_id}"): "payment provider, HMAC-verified per tool",
    ("GET", "/health"): "public liveness",
    ("GET", "/health/detail"): "operator view (5.7 will restrict)",
    ("GET", "/metrics"): "token-protected metrics",
    ("GET", "/test"): "static WebRTC test page",
    ("GET", "/invitations/{token}"): "reached by invitation token, which names its own organisation",
    ("POST", "/invitations/{token}/accept"): "reached by invitation token, which names its own organisation",
}


def _reaches(dependant, targets, seen=None) -> bool:
    if seen is None:
        seen = set()
    if id(dependant) in seen:
        return False
    seen.add(id(dependant))
    return any(d.call in targets or _reaches(d, targets, seen) for d in dependant.dependencies)


def _live_api_routes():
    """Every real APIRoute the running app would actually dispatch to,
    flattened out of FastAPI's lazy `_IncludedRouter` wrappers."""
    for route_context in iter_route_contexts(app.routes):
        route = route_context.original_route
        if isinstance(route, APIRoute):
            yield route


def test_every_route_checks_the_organisation_or_is_exempt_for_a_reason():
    targets = {get_org_context, get_org_context_from_path}
    unchecked = []
    for route in _live_api_routes():
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) in EXEMPT:
                continue
            if not _reaches(route.dependant, targets):
                unchecked.append(f"{method} {route.path}")
    assert unchecked == [], f"routes without the organisation check: {unchecked}"


def test_the_exempt_list_has_no_stale_entries():
    live = {(m, r.path) for r in _live_api_routes() for m in r.methods}
    assert set(EXEMPT) <= live, set(EXEMPT) - live
