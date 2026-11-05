"""Outbound request guard for caller-supplied URLs.

Three endpoints let a caller hand the server a URL and have the server fetch it:
the model, data-source and MCP connection tests. On ECS/Fargate that is a
credential-disclosure primitive, not just a reachability one -- the task role's
keys are served over plain HTTP from 169.254.170.2, and the instance metadata
service from 169.254.169.254. A request to either returns usable credentials in
the response body, which these endpoints then echo back to the caller.

So a scheme check is not enough, and neither is a hostname check: a public
hostname can resolve to 169.254.170.2, and a public URL can answer 302 with a
metadata address in Location. This module checks every address a name resolves
to and refuses to follow redirects.

Private ranges (10/8, 172.16/12, 192.168/16) are a different case: a self-hosted
Ollama or OpenAI-compatible endpoint inside the VPC is a legitimate target. Those
are allowed only when AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS is set to "true".
Link-local, loopback, multicast and reserved addresses are never allowed,
whatever that variable says, because no legitimate model endpoint lives there.

Residual risk, stated rather than hidden: validation resolves the name and the
connection resolves it again, so a name that changes answers between the two
calls (DNS rebinding) is not covered. Closing that needs the connection pinned
to the validated address, which urllib does not expose. The redirect refusal and
the response-size cap below are what keep the remaining window narrow.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.request
import urllib.error
from urllib.parse import urlsplit

# 10 MB. These are connection *tests*; nothing legitimate returns more, and an
# unbounded read against an attacker-chosen URL is a memory-exhaustion lever.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

ALLOWED_SCHEMES = ("http", "https")


class BlockedRequestError(ValueError):
    """A caller-supplied URL was refused. Message is safe to return to the caller."""


def _allow_private() -> bool:
    return os.environ.get("AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS", "").strip().lower() == "true"


def _unwrap(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Reduce IPv6 forms that embed an IPv4 address to that address.

    ::ffff:169.254.169.254 reaches the metadata service while looking nothing
    like it, and the IPv6 object's own flags do not reflect the embedded value.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour):
            if embedded is not None:
                return embedded
        if ip.teredo is not None:
            return ip.teredo[1]
    return ip


def _check_ip(raw: str) -> None:
    ip = _unwrap(ipaddress.ip_address(raw))

    # Never allowed, regardless of configuration.
    if ip.is_loopback:
        raise BlockedRequestError("URL resolves to a loopback address")
    if ip.is_link_local:
        # 169.254.0.0/16 and fe80::/10 -- the metadata and task-credential endpoints.
        raise BlockedRequestError("URL resolves to a link-local address")
    if ip.is_multicast:
        raise BlockedRequestError("URL resolves to a multicast address")
    if ip.is_unspecified:
        raise BlockedRequestError("URL resolves to an unspecified address")
    if ip.is_reserved:
        raise BlockedRequestError("URL resolves to a reserved address")

    if not ip.is_global and not _allow_private():
        raise BlockedRequestError(
            "URL resolves to a private address. Set "
            "AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS=true to permit endpoints inside the VPC."
        )


def validate_outbound_url(url: str) -> str:
    """Return the URL if the server may fetch it; raise BlockedRequestError if not."""
    if not url or not isinstance(url, str):
        raise BlockedRequestError("URL is required")
    url = url.strip()

    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedRequestError("URL must use http:// or https://")
    # Credentials in the URL are how an open redirect gets turned into a
    # request that carries someone else's authentication.
    if parts.username or parts.password:
        raise BlockedRequestError("URL must not contain credentials")

    host = parts.hostname
    if not host:
        raise BlockedRequestError("URL has no host")

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                  proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedRequestError(f"Host could not be resolved: {host}") from exc

    if not infos:
        raise BlockedRequestError(f"Host could not be resolved: {host}")

    # Every answer must pass. One bad address is enough to reach the metadata
    # service, because which address gets used is not ours to decide.
    for info in infos:
        _check_ip(info[4][0])

    return url


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects instead of following them.

    A validated public URL that answers 302 with a metadata address in Location
    defeats the whole check, and urllib follows redirects by default.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BlockedRequestError(f"Refusing to follow a redirect to {newurl[:200]}")


_opener = urllib.request.build_opener(_NoRedirects)


def safe_urlopen(target, timeout: int = 10):
    """urlopen for a caller-supplied target, with the URL validated and redirects refused.

    Accepts a str or a urllib Request. Raises BlockedRequestError if the URL is
    not one the server may fetch.
    """
    if isinstance(target, urllib.request.Request):
        validate_outbound_url(target.full_url)
    else:
        target = validate_outbound_url(target)
    return _opener.open(target, timeout=timeout)


def safe_read(target, timeout: int = 10, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """safe_urlopen plus a bounded read."""
    with safe_urlopen(target, timeout=timeout) as resp:
        return resp.read(limit)
