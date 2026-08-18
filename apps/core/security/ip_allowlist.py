"""IP allowlist enforcement for admin URL prefixes.

Source addresses come from `apps.core.security.client_ip`, so behind a
configured reverse proxy this compares the real caller rather than the
proxy. With no proxy configured it is `REMOTE_ADDR` — which means that
behind an UNCONFIGURED proxy the allowlist would compare every caller
against the proxy's own address and lock the operator out. Configure
`TRUSTED_PROXY_IP_HEADER`/`TRUSTED_PROXY_IPS` before setting this.

`ADMIN_IP_ALLOWLIST` is a list of CIDRs (e.g. `["10.0.0.0/24",
"203.0.113.5/32"]`). When non-empty, only those source IPs may reach
`<ADMIN_PANEL_URL_PATH>` or `<DJANGO_ADMIN_URL_PATH>` paths; everyone
else gets a generic 404 (NOT 403 — we don't reveal that the URL
exists).

Empty list = disabled (default). This is intentional: in dev there's
no operator network, and locking down the admin URL by IP before the
operator has set things up would lock them out of their own dev
machine. Prod operators set the var explicitly.

Why a middleware (not a per-view decorator):
- `<DJANGO_ADMIN_URL_PATH>` mounts `django.contrib.admin.site.urls` —
  we don't own those views and can't easily wrap them with a
  decorator. A middleware fires before URL dispatch.
- `<ADMIN_PANEL_URL_PATH>` is the placeholder until Phase 10.2; same
  problem when that lands as a real admin app.
- Staff 2FA URLs at `<ADMIN_PANEL_URL_PATH>2fa/...` ALSO fall under
  the prefix — by design, the IP gate is a perimeter check that
  wraps every admin-tier route uniformly.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound

from apps.core.security.client_ip import client_ip

log = logging.getLogger(__name__)


def _normalize_cidrs(raw: object) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse `ADMIN_IP_ALLOWLIST` into a list of network objects.

    Accepts plain IPs (`"10.0.0.5"` → /32) and CIDRs (`"10.0.0.0/24"`).
    Malformed entries are logged + dropped — we'd rather lock the
    operator out by accident than silently accept a typo that opens
    the gate.
    """
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if not raw:
        return out
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    for entry in items:
        try:
            out.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError:
            log.warning("ADMIN_IP_ALLOWLIST: ignoring invalid entry %r", entry)
    return out


def is_ip_allowed(ip: str | None) -> bool:
    """True when no allowlist is configured OR `ip` is inside one of
    the configured CIDRs. Empty/invalid `ip` returns False when an
    allowlist exists — better to refuse than to fall through.

    Reads through `apps.core.runtime_settings` so admin Settings page
    edits take effect without a redeploy. Falls through to
    `settings.ADMIN_IP_ALLOWLIST` when no runtime override is set.
    """
    from apps.core import runtime_settings

    nets = _normalize_cidrs(runtime_settings.get_admin_ip_allowlist())
    if not nets:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def _admin_path_prefixes() -> list[str]:
    """Return the URL prefixes the allowlist guards.

    Both ADMIN_PANEL_URL_PATH (custom panel) and DJANGO_ADMIN_URL_PATH
    (Django admin fallback), plus any IP_ALLOWLIST_EXTRA_PREFIXES
    (e.g. `docs/` — staff-only pages that must 404 for outsiders
    rather than redirect and leak the admin slug). Each is normalized
    with leading + trailing slash so a request to `/admin` (no slash)
    doesn't slip past while `/admin/` is guarded.
    """
    paths: list[str] = []
    for raw in (
        getattr(settings, "ADMIN_PANEL_URL_PATH", ""),
        getattr(settings, "DJANGO_ADMIN_URL_PATH", ""),
        *getattr(settings, "IP_ALLOWLIST_EXTRA_PREFIXES", []),
    ):
        if not raw:
            continue
        prefix = "/" + raw.strip("/") + "/"
        paths.append(prefix)
    return paths


def _is_admin_request(path: str) -> bool:
    return any(path.startswith(p) for p in _admin_path_prefixes())


class AdminIPAllowlistMiddleware:
    """Async-only. Refuses admin-prefix requests from non-allowlisted IPs.

    Position in MIDDLEWARE: high — after `RequestIdMiddleware` (so the
    refusal carries a request_id) and after any auth-signal middleware
    that shouldn't fire for blocked traffic, but BEFORE the auth +
    security stack so a banned IP can't drive the lockout/2FA paths.
    Returns 404 (not 403) so a scanning attacker can't tell the
    difference between "URL doesn't exist" and "URL exists but you
    can't reach it".
    """

    sync_capable = False
    async_capable = True

    def __init__(
        self,
        get_response: Callable[[HttpRequest], Awaitable[HttpResponse]],
    ) -> None:
        self.get_response = get_response
        markcoroutinefunction(self)
        if not iscoroutinefunction(get_response):
            raise RuntimeError(
                "AdminIPAllowlistMiddleware requires the async ASGI stack."
            )

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path or ""
        if _is_admin_request(path):
            ip = client_ip(request)
            # is_ip_allowed reads the DB (runtime override) — must hop to a
            # thread or the ORM raises SynchronousOnlyOperation under ASGI
            # and the override silently never loads.
            from asgiref.sync import sync_to_async

            if not await sync_to_async(is_ip_allowed)(ip):
                log.warning(
                    "admin IP allowlist: refusing path=%s ip=%s",
                    path,
                    ip,
                )
                return HttpResponseNotFound("Not Found")
        return await self.get_response(request)


__all__ = ["AdminIPAllowlistMiddleware", "is_ip_allowed"]
