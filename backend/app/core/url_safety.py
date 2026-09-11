"""Where this system is allowed to send a request it did not choose itself.

Two Phase 3 features take a URL from outside and then make THE SERVER fetch
it:

  - a webhook subscription's URL (task 3.8), typed into the dashboard
  - a bot tool's URL (task 3.1), typed into the tool form

Both are ordinary, intended features, and both are also the exact shape of
a server-side request forgery: the address is chosen by somebody else and
the request originates from inside this network. `http://169.254.169.254/`
is not a request to the internet at all — on a cloud VM (this one included,
a GCP instance) that is the metadata service, and it hands the instance's
own service-account token to anything on the box that asks. `http://
127.0.0.1:27017` is the database. Neither is reachable from outside, which
is precisely why being able to make the server fetch them is worth
something to an attacker.

"But the customer typed it themselves" is not a defence. On the tool path
the response is read aloud to a caller who is not the customer; on the
webhook path nothing needs to be read back at all — a signed POST into an
internal endpoint is the damage. And a customer account is one credential
away from not being the customer.

So a target must resolve to a PUBLIC address. Loopback, the private
ranges, link-local (which is where the metadata service lives), multicast
and the reserved blocks are all refused.

Two design points, both CHANGED by review finding I4 (2026-09-10). What
they used to say is kept here, because what was wrong with them is the
most useful thing this file can explain.

  - **Fails CLOSED when DNS cannot answer.** This used to fail open, and
    the reasoning read well: a name that does not resolve is not a route
    into anything, so why turn a resolver blip into a delivery outage?

    The flaw is who owns the resolver. The name belongs to whoever chose
    the URL, and so does the nameserver that answers for it. Making the
    lookup fail — SERVFAIL, or just never answering — is free for that
    person. So the attack was never "point it at 127.0.0.1", which was
    caught; it was "make the check unable to run", after which the URL
    passed unexamined and the real request went out to whatever the
    nameserver felt like saying next. A check that passes when it could
    not run is not a check. An unresolvable name is now a rejection.

  - **Resolved ONCE, and dialled by number.** "Checked again immediately
    before the request" was still a check-then-connect: validation called
    getaddrinfo, httpx then called getaddrinfo again when it opened the
    socket, and nothing tied the second answer to the first. A nameserver
    that answers the first query with a public address and the second with
    169.254.169.254 passes the check and reaches the metadata service
    anyway. Re-checking earlier is still earlier.

    So the address that passed the check is now the address that is
    connected to, by number — see PinnedPublicIPTransport below. The
    hostname still travels with the request (Host header, TLS SNI,
    certificate verification), so nothing about pinning weakens TLS or
    breaks virtual hosting; it only removes the second lookup.

Failing closed does have a cost, and it is the one the old comment named:
a customer whose DNS is genuinely broken cannot save a webhook URL, and a
delivery attempted during a resolver outage is refused rather than
attempted. That is the right way round for this trade — a refused
delivery is retried by the outbox, while a request sent unchecked cannot
be taken back.

`allow_private_outbound_urls` exists for local development, where pointing
a tool at `http://localhost:9000` is the normal thing to do. It defaults to
off and should stay off anywhere reachable from the internet.
"""

import asyncio
import ipaddress
import socket
import ssl
from functools import lru_cache
from urllib.parse import urlparse

import httpx

from app.core.config import settings

ALLOWED_SCHEMES = {"http", "https"}

PRIVATE_ADDRESS_REASON = (
    "That address is on a private, loopback, or link-local network, "
    "which this server will not send requests to. Use a publicly "
    "reachable URL."
)

# Deliberately does not distinguish "the resolver failed" from "the name
# has no addresses". Both mean the same thing here — this server could not
# establish where the request would actually go — and the person on the
# other side of this message is as likely to be probing as troubleshooting.
UNRESOLVABLE_REASON = (
    "That host name could not be resolved to an address, so this server "
    "cannot confirm the request would not reach a private network. Check "
    "the host name and try again."
)


class BlockedAddress(Exception):
    """Raised at connect time when the address a request would reach is not
    one this server is willing to send to.

    An exception rather than a returned reason because it is raised from
    inside an httpx transport, where returning "no" is not available — the
    contract there is a Response or a raise. Both call sites catch it
    explicitly and turn it back into their own refusal shape (see
    services/tool_registry.py and services/webhooks.py); neither lets it
    escape as a generic failure, because "we refused to send this" and
    "their server is down" are different facts and the logs need to keep
    them apart.
    """


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is somewhere on the actual internet."""
    # ::ffff:127.0.0.1 is a loopback address wearing an IPv6 costume: the
    # v6 object reports itself as global, while the address the socket
    # actually dials is the v4 one inside it. Unwrap before judging.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_is_public(mapped)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_addresses(host: str) -> list[str]:
    """Every address this name currently answers with. Raises OSError.

    Its own function so there is exactly one place name resolution
    happens, which is also the seam the tests script to make a rebinding
    attack reproducible — a name that answers differently the second time.
    """
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The host as an address if it already is one, else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def resolve_public_address(host: str) -> tuple[str | None, str | None]:
    """Where this name may be dialled, or why it may not be.

    Returns (address, None) when the request may proceed — and that
    address is the one to connect to, not merely evidence that connecting
    is allowed. Returns (None, reason) otherwise.

    Every address the name resolves to has to pass, not just the first: a
    name that returns one public and one loopback address is a deliberate
    attack shape, and which one gets dialled is not ours to predict. An
    empty answer and a failed lookup are both rejections — see the module
    docstring on failing closed.
    """
    literal = _literal_ip(host)
    if literal is not None:
        return (host, None) if _ip_is_public(literal) else (None, PRIVATE_ADDRESS_REASON)

    try:
        addresses = _resolve_addresses(host)
    except OSError:
        return None, UNRESOLVABLE_REASON

    usable: list[str] = []
    for raw in addresses:
        ip = _literal_ip(raw)
        if ip is None:
            continue
        if not _ip_is_public(ip):
            return None, PRIVATE_ADDRESS_REASON
        usable.append(raw)

    if not usable:
        return None, UNRESOLVABLE_REASON
    # The first one, which is the order the resolver returned and therefore
    # the one the OS would have picked anyway.
    return usable[0], None


def rejection_reason(raw: str) -> str | None:
    """None if this URL is safe to request, else a sentence saying why not.

    Returns the reason rather than raising so each caller can decide what
    that means for it — a 422 at registration, a logged refusal at
    delivery time.
    """
    url = (raw or "").strip()
    if not url:
        return "A URL is required."

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return "The URL must start with http:// or https://."
    if not parsed.hostname:
        return "The URL does not contain a host name."

    if settings.allow_private_outbound_urls:
        return None

    # Review finding I4 — the reason is returned whatever it is. This used
    # to allow a name through when resolution failed; see the module
    # docstring for why that was the hole rather than a kindness.
    _address, reason = resolve_public_address(parsed.hostname)
    return reason


def is_safe(raw: str) -> bool:
    return rejection_reason(raw) is None


class PinnedPublicIPTransport(httpx.AsyncHTTPTransport):
    """An httpx transport that resolves each request itself, refuses what
    it does not like, and then connects to that exact address.

    Review finding I4 (2026-09-10). Validating a URL and then handing it to
    httpx means the name is resolved twice — once by the check, once by the
    socket — and only the first answer is ever examined. A nameserver that
    answers differently the second time (DNS rebinding) passes validation
    and reaches 169.254.169.254 anyway. The gap is not a timing bug that
    could be narrowed by checking later; it is structural, because two
    lookups are two questions and the attacker gets to answer both.

    Closing it means the component that opens the socket has to be the one
    that decides, so the decision and the connection cannot disagree. That
    is this class: `handle_async_request` resolves the host, applies the
    same public-address rule the rest of this module applies, and then
    rewrites the request to dial that address by number.

    Three things travel with the request so pinning stays invisible to
    everything else:

      - the Host header keeps the original name, or virtual-hosted sites
        (which is most of them) serve the wrong site or none;
      - `sni_hostname` keeps the original name, so TLS negotiates for the
        site the customer configured;
      - because SNI is what certificate verification checks against, the
        certificate is still verified against the NAME, not the address.
        Pinning does not quietly downgrade TLS, which would have traded
        one vulnerability for another.

    Redirects get this for free: tool_registry follows them by re-issuing
    a request through the same client, so every hop is resolved and judged
    here as well as by its own rejection_reason check.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # The documented local-development escape hatch: pointing a tool at
        # http://localhost:9000 is the normal thing to do there. Off by
        # default and meant to stay off anywhere reachable.
        if settings.allow_private_outbound_urls:
            return await super().handle_async_request(request)

        host = request.url.host
        # getaddrinfo blocks, and this runs on the API process's event
        # loop — a resolver that is not answering would otherwise stall
        # every other request in the process alongside this one.
        address, reason = await asyncio.to_thread(resolve_public_address, host)
        if reason is not None:
            raise BlockedAddress(f"{host}: {reason}")

        if address != host:
            # Host header first: httpx set it from the original URL when the
            # request was built, and it must keep saying the name even
            # though the URL is about to say the address.
            request.headers["Host"] = request.url.netloc.decode("ascii")
            request.url = request.url.copy_with(host=address)
            request.extensions = dict(request.extensions)
            request.extensions["sni_hostname"] = host

        return await super().handle_async_request(request)


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Built once for the life of the process, and reused.

    Not an optimisation detail — measured at 0.17s to construct on this
    machine, because it reads and parses the whole CA bundle. A tool call
    happens in the middle of a live phone call, and paying a sixth of a
    second of silence per call to re-read a file that has not changed is
    exactly the kind of latency this project has spent real effort
    removing. The context is immutable once built and safe to share.

    The context only, NOT the transport: a transport owns a connection
    pool, and `async with httpx.AsyncClient(...)` closes the transport it
    was given on the way out, so a shared one would be closed underneath
    the next caller.
    """
    return httpx.create_ssl_context()


def safe_transport() -> PinnedPublicIPTransport:
    """The transport every outbound request built from a customer-supplied
    URL must go through. A named function so the two call sites read as
    using one policy rather than each constructing their own."""
    return PinnedPublicIPTransport(verify=_ssl_context())
