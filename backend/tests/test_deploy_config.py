"""Structural checks on the deployment config that the app itself can't enforce.

Some production-safety properties live in deploy/ files, not in Python, so no
behavioural test can reach them — pytest talks to the ASGI app directly and
never goes near uvicorn's process args or the compose network. These parse
the config files and assert the invariants that, if silently dropped, would
reintroduce a live incident.
"""

import json
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _dockerfile() -> str:
    return (DEPLOY / "Dockerfile").read_text(encoding="utf-8")


def _cmd_tokens() -> list[str]:
    """The exec-form CMD in deploy/Dockerfile, parsed to its token list.

    Matched to the JSON array specifically so prose in a nearby comment
    that happens to mention a flag can't satisfy the assertions below.
    """
    m = re.search(r'^CMD\s*(\[[^\]]*\])', _dockerfile(), re.M)
    assert m, "no exec-form CMD found in deploy/Dockerfile"
    return json.loads(m.group(1))


def _compose() -> str:
    return (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")


def test_uvicorn_trusts_the_reverse_proxy_for_forwarded_headers():
    """Without --forwarded-allow-ips, uvicorn only rewrites request.client
    from X-Forwarded-For when the immediate peer is 127.0.0.1. The only peer
    here is Caddy, from its container address on the compose bridge, which is
    not loopback — so every unauthenticated request collapsed to Caddy's one
    IP and slowapi's per-IP fallback (app/core/rate_limit.py) became a single
    global bucket: five failed logins a minute from anyone locked out login
    for the whole platform. Reviewed and confirmed 2026-09-10.
    """
    tokens = _cmd_tokens()
    assert "--forwarded-allow-ips" in tokens, (
        "uvicorn is not told to trust the reverse proxy's X-Forwarded-For, "
        "so per-IP rate limiting collapses to one global bucket in production"
    )
    # The value must be the very next token, and non-empty — a bare flag is
    # a startup error uvicorn would reject, but catch it here not in a deploy.
    i = tokens.index("--forwarded-allow-ips")
    assert i + 1 < len(tokens) and tokens[i + 1], "--forwarded-allow-ips has no value"


def test_the_backend_port_is_never_published_to_the_host():
    """--forwarded-allow-ips="*" (trust any peer's X-Forwarded-For) is only
    safe because the sole thing that can reach uvicorn is Caddy. The moment
    port 8080 is published to the host, an attacker can hit it directly and
    spoof X-Forwarded-For to forge any client IP — defeating rate limiting a
    second way. `expose:` keeps the port on the compose network only;
    `ports:` would publish it.
    """
    compose = _compose()
    backend = re.search(r"\n  backend:\n(.*?)(?=\n  [a-z]|\Z)", compose, re.S)
    assert backend, "could not find the backend service in docker-compose.yml"
    body = backend.group(1)
    assert "expose:" in body, "backend should EXPOSE 8080 to the compose network only"
    assert not re.search(r"\n\s+ports:", body), (
        "backend publishes a host port — uvicorn would then trust a spoofable "
        "X-Forwarded-For from the internet (see the sibling test)"
    )
