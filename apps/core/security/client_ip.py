"""Real client IP resolution behind a reverse proxy.

Every perimeter control that keys on "who is calling" — the admin-login
lockout, the honeypot counter, the admin IP allowlist, the API rate-limit
log, the exposure health check — used to read `REMOTE_ADDR` directly.
Behind a proxy that is the PROXY's address, not the caller's, and the
failure mode is worse than a wrong string in a log:

- The admin-login lockout (`apps.core.security.auth_signals`) buckets
  failures per IP. When every request carries the same proxy address the
  operator and the attacker share one bucket, so a brute-force attempt
  locks the OPERATOR out while the attacker moves to the next edge node
  and continues. The control inverts.
- Per-IP honeypot counters and rate-limit buckets collapse into one
  global bucket, where a single abusive caller throttles everyone.

So this module resolves the caller's address from a proxy header — but
ONLY when the request actually arrived from a proxy we trust, because
otherwise the header is just attacker-supplied text. `health.py` used to
read `X-Forwarded-For` unconditionally, which is exactly the spoofable
shape this replaces.

Both halves are required:

    TRUSTED_PROXY_IP_HEADER = "CF-Connecting-IP"
    TRUSTED_PROXY_IPS       = ["10.0.0.0/8"]     # the TCP peer, see below

`TRUSTED_PROXY_IPS` is matched against `REMOTE_ADDR` — whatever actually
opens the TCP connection to this process, which is **not** necessarily
the CDN. Behind Coolify/Traefik the peer is the Traefik container on the
Docker network (a private address); Cloudflare is one hop further out.
The exposure panel on the health page prints the observed peer so the
value can be read off a real request rather than guessed.

Setting only one half is a silent half-configuration — the header would
be ignored and the operator would believe per-IP controls work — so
`assert_prod_safe()` refuses boot on it.

Default is both-unset, which returns `REMOTE_ADDR` exactly as before.
Nothing starts trusting a header merely because someone deployed behind
a proxy.
"""
from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache
from typing import Iterable

from django.conf import settings
from django.http import HttpRequest

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_networks(raw: Iterable[str]) -> tuple[Network, ...]:
    """Parse CIDR strings, dropping (and logging) malformed entries.

    A typo must not widen trust: an unparseable entry is discarded, so
    the worst case is that the header stops being trusted and callers
    look like the proxy again — the pre-existing behaviour.
    """
    out: list[Network] = []
    for entry in raw or ():
        try:
            out.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError:
            log.warning("TRUSTED_PROXY_IPS: ignoring invalid entry %r", entry)
    return tuple(out)


@lru_cache(maxsize=1)
def _cached_networks(raw: tuple[str, ...]) -> tuple[Network, ...]:
    """Parse once per distinct setting value — this runs on every request."""
    return _parse_networks(raw)


def trusted_proxy_networks() -> tuple[Network, ...]:
    raw = tuple(str(x) for x in (getattr(settings, "TRUSTED_PROXY_IPS", None) or ()))
    return _cached_networks(raw)


def trusted_proxy_header() -> str:
    return (getattr(settings, "TRUSTED_PROXY_IP_HEADER", "") or "").strip()


def is_trusted_peer(remote_addr: str | None) -> bool:
    """True when `remote_addr` is one of the configured proxy addresses."""
    nets = trusted_proxy_networks()
    if not nets or not remote_addr:
        return False
    try:
        addr = ipaddress.ip_address(remote_addr.strip())
    except ValueError:
        return False
    return any(addr in net for net in nets)


def resolve(
    remote_addr: str | None,
    header_value: str | None,
    *,
    trusted: bool,
) -> str | None:
    """Pure resolution step: which address do we treat as the caller's?

    Falls back to `remote_addr` whenever the header cannot be trusted or
    cannot be believed. Every rejection path ends at the TCP peer, which
    is always true even when it is not useful.
    """
    if not trusted or not header_value:
        return remote_addr or None
    # `X-Forwarded-For` style chains list the original client leftmost and
    # append hops. `CF-Connecting-IP` carries a single address, which the
    # same rule handles unchanged.
    candidate = header_value.split(",")[0].strip()
    if not candidate:
        return remote_addr or None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        # A trusted proxy sending a non-IP means the proxy is misconfigured,
        # not that the caller is spoofing. Log it — silently falling back
        # would leave per-IP controls quietly degraded.
        log.warning(
            "%s from trusted proxy %s is not an IP address: %r",
            trusted_proxy_header() or "proxy header",
            remote_addr,
            candidate[:100],
        )
        return remote_addr or None
    return candidate


def client_ip(request: HttpRequest) -> str | None:
    """The caller's address: the trusted proxy header when configured and
    the request came from a trusted peer, else the TCP peer."""
    remote_addr = request.META.get("REMOTE_ADDR") or None
    header = trusted_proxy_header()
    if not header:
        return remote_addr
    return resolve(
        remote_addr,
        request.headers.get(header),
        trusted=is_trusted_peer(remote_addr),
    )


def peer_ip(request: HttpRequest) -> str | None:
    """The raw TCP peer, ignoring any proxy header.

    Only for diagnostics — the health page prints this so an operator can
    read the value to put in `TRUSTED_PROXY_IPS` off a real request.
    """
    return request.META.get("REMOTE_ADDR") or None


__all__ = [
    "client_ip",
    "peer_ip",
    "is_trusted_peer",
    "resolve",
    "trusted_proxy_header",
    "trusted_proxy_networks",
]
