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

Two deliberate design points:

  - **Fails OPEN when DNS cannot answer.** A name that does not resolve is
    not a route into anything — the request that follows simply fails — so
    treating a resolver blip as an attack would turn it into a delivery
    outage for no security gain.

  - **Checked again at request time, not only when saved.** A name that
    resolved publicly when the customer saved it can be re-pointed at
    127.0.0.1 afterwards (this is DNS rebinding, and it is not exotic).
    The check that matters is the one immediately before the request.

`allow_private_outbound_urls` exists for local development, where pointing
a tool at `http://localhost:9000` is the normal thing to do. It defaults to
off and should stay off anywhere reachable from the internet.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.config import settings

ALLOWED_SCHEMES = {"http", "https"}


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


def _host_is_public(host: str) -> bool | None:
    """True, False, or None when DNS could not answer (see module docstring).

    Every address the name resolves to has to pass, not just the first:
    a name that returns one public and one loopback address is a
    deliberate attack shape, and which one gets dialled is not ours to
    predict.
    """
    try:
        return _ip_is_public(ipaddress.ip_address(host))
    except ValueError:
        pass  # not a literal address — resolve it below

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return None

    seen = False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        seen = True
        if not _ip_is_public(ip):
            return False
    return True if seen else None


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

    public = _host_is_public(parsed.hostname)
    if public is False:
        return (
            "That address is on a private, loopback, or link-local network, "
            "which this server will not send requests to. Use a publicly "
            "reachable URL."
        )
    # None (DNS could not answer) is allowed through deliberately.
    return None


def is_safe(raw: str) -> bool:
    return rejection_reason(raw) is None
