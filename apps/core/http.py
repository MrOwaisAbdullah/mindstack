"""Safe outbound HTTP helper.

Endpoint authors call user-supplied URLs all the time — the docs Try-it
panel hits an MCP tool that fetches a URL, summarize_url passes its arg
into requests, etc. The naive `httpx.get(url)` pattern has three known
classes of bug:

1. **SSRF** — a user supplies `http://169.254.169.254/...` (AWS IMDS)
   or `http://localhost:6379/` (the same Redis the app talks to) and
   the server happily fetches it back into the response body. We
   block: private/loopback/link-local/multicast targets resolved at
   request time (re-resolution defends against DNS rebinding in the
   common case — operators who need stronger guarantees front the
   process with an egress firewall).
2. **Schema injection** — `file:///etc/passwd` or `gopher://` slipping
   through. We require `http`/`https` only.
3. **Memory exhaustion** — a 5 GB response body buffered into RAM.
   We stream the body and abort once `max_size` bytes have arrived.

The helper shape mirrors httpx's own `request()` so endpoint authors
can drop it in without learning a new API.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

# 5 MB default body cap — same as `DATA_UPLOAD_MAX_MEMORY_SIZE`. Endpoint
# authors handling larger payloads (CSV imports, image generation) bump
# per-call. We never default to "unlimited" because a single rogue call
# can OOM a worker.
DEFAULT_MAX_SIZE_BYTES = 5 * 1024 * 1024

# Hard timeout caps the request budget end-to-end (connect + read + write).
# Long-running endpoints should enqueue a Q2 job rather than block the
# request thread for >10s.
DEFAULT_TIMEOUT_SECONDS = 10.0

# `long_request` defaults — tuned for outbound AI provider calls
# (Replicate, fal.ai, OpenAI, Anthropic) made FROM A Q2 WORKER, never from
# a request thread. 5 minutes covers most image/video/audio generations
# without rewarding upstreams that hang silently; 50 MB caps the result
# body for generation responses that inline base64-encoded media.
LONG_REQUEST_TIMEOUT_SECONDS = 300.0
LONG_REQUEST_MAX_SIZE_BYTES = 50 * 1024 * 1024


class UnsafeURL(ValueError):
    """Raised when a URL targets a private/loopback/link-local IP, an
    unsupported scheme, or fails to resolve. Endpoint authors catch
    this + return a 400 to the caller."""


class ResponseTooLarge(ValueError):
    """Raised when the server starts streaming back more than `max_size`
    bytes. Helper aborts the connection and re-raises."""


def safe_request(
    method: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_size: int = DEFAULT_MAX_SIZE_BYTES,
    allow_private_ips: bool = False,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """SSRF-guarded, size-bounded outbound HTTP. Same return shape as
    `httpx.request()`.

    `allow_private_ips=True` is an explicit escape hatch — internal
    integrations (e.g. talking to a sidecar `localhost:9000` service
    inside the same pod) flip it on with eyes open. We never silently
    permit private IPs based on hostname allow-lists because DNS
    rebinding makes that lookup-time check meaningless.

    Other `**kwargs` flow through to `httpx.stream()` unchanged
    (`follow_redirects=`, `auth=`, etc.). Redirects are followed by
    default-False because each hop needs a fresh SSRF check; honor
    that contract by only setting `follow_redirects=True` after
    you've thought about whether the redirected target is safe.
    """
    _check_url(url, allow_private_ips=allow_private_ips)

    request_headers = dict(headers or {})

    with httpx.Client(timeout=timeout) as client:
        with client.stream(method, url, headers=request_headers, **kwargs) as response:
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > max_size:
                    raise ResponseTooLarge(
                        f"response body exceeded {max_size} bytes"
                    )
            response.read = lambda: bytes(content)  # type: ignore[method-assign]
            # httpx populates `response.content` lazily via `read()`.
            # Stash the buffered bytes so callers' `.text` / `.json()`
            # work without a second network round-trip.
            response._content = bytes(content)  # type: ignore[attr-defined]
    return response


def long_request(
    method: str,
    url: str,
    *,
    timeout: float = LONG_REQUEST_TIMEOUT_SECONDS,
    max_size: int = LONG_REQUEST_MAX_SIZE_BYTES,
    allow_private_ips: bool = False,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Outbound HTTP for long-running upstream calls (UPDATES.md #8).

    Thin alias around `safe_request` with AI-provider-friendly defaults:
    300s timeout (vs. `safe_request`'s 10s) and 50 MB max body (vs. 5 MB).
    Same SSRF + scheme + size guards apply — only the defaults differ.

    Use from inside a Q2 task body when the upstream is a generation API
    (Replicate, fal.ai, OpenAI, Anthropic, Vidtsx) that can legitimately
    take minutes. Do NOT use from a sync REST endpoint's `run()` body —
    the request thread blocks for the whole duration and the per-request
    `WEB_TIMEOUT` will kill it. Enqueue a Q2 task instead (Pattern 3 in
    CLAUDE.md → `apps/app_endpoints/generate_image/`).

    Operators tuning per-call: pass `timeout=N` / `max_size=N` to override
    the long-running defaults. Use `safe_request` directly when you want
    the short-call defaults.
    """
    return safe_request(
        method,
        url,
        timeout=timeout,
        max_size=max_size,
        allow_private_ips=allow_private_ips,
        headers=headers,
        **kwargs,
    )


def _check_url(url: str, *, allow_private_ips: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURL(f"only http/https are allowed; got {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeURL("URL missing host")

    if allow_private_ips:
        return

    # Resolve every A/AAAA record — a hostname that resolves to multiple
    # IPs (round-robin DNS, IPv4+IPv6 dual-stack) needs every target
    # checked, otherwise an attacker with control of one record gets in.
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"DNS resolution failed: {exc}") from exc

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise UnsafeURL(f"invalid resolved IP: {ip_str!r}") from None
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise UnsafeURL(
                f"refusing to fetch URL that resolves to non-public IP {ip_str}"
            )
        if ip.is_reserved or ip.is_unspecified:
            raise UnsafeURL(
                f"refusing to fetch URL that resolves to reserved IP {ip_str}"
            )


__all__ = [
    "DEFAULT_MAX_SIZE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "LONG_REQUEST_MAX_SIZE_BYTES",
    "LONG_REQUEST_TIMEOUT_SECONDS",
    "ResponseTooLarge",
    "UnsafeURL",
    "long_request",
    "safe_request",
]
