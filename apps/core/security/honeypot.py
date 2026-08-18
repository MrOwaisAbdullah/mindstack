"""Honeypot middleware.

Bot scanners hammer well-known admin paths regardless of whether the
target is running WordPress, phpMyAdmin, or any other framework. We
catch those probes at the middleware layer:

- Returns a generic `404 Not Found` (no version banner — Django's
  default 404 doesn't expose framework info when DEBUG=False).
- Writes one `AuditEvent` row with `action="security.honeypot.hit"`
  carrying the hit path + IP + UA so the admin Errors / Audit panels
  surface scanner activity for triage.
- Optional auto-blocklist after N hits — wired through Phase 9.2's
  `lockout` primitive scoped under `honeypot-ip`. After 3 hits in
  60s, the IP enters a 1h soft lockout that any future request from
  the same IP can read via `is_locked("honeypot-ip", ip)`. We don't
  enforce that blocklist globally yet (it's a noisy signal — better
  used as input to manual ops review); the counter just exists.

The path list is a closed allowlist of common scanner targets. We
deliberately don't open it up to a regex / config because that
attracts false positives — every "this looks suspicious" rule that
ships in core is one more thing operators have to override when
they hit a corner case.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound

from apps.core import audit_hook
from apps.core.security.client_ip import client_ip
from apps.core.security.lockout import is_locked, record_failure

log = logging.getLogger(__name__)


# Closed list of well-known scanner targets. Path-prefix match (so e.g.
# `/wp-admin/setup-config.php` is caught by the `/wp-admin/` prefix).
# Trailing slash is significant — `/admin/login/` is the bot probe, but
# `/admin/2fa/setup/` (our staff-2FA flow under the configurable admin
# URL) MUST NOT match. The `_is_honeypot_path` helper handles that.
_HONEYPOT_PATHS: frozenset[str] = frozenset(
    {
        # WordPress
        "/wp-admin/",
        "/wp-login.php",
        "/wp-config.php",
        "/wp-content/",
        # phpMyAdmin (case variants — bots try both)
        "/phpmyadmin/",
        "/phpMyAdmin/",
        "/PMA/",
        "/pma/",
        # Other CMSes / control panels frequently fingerprinted
        "/administrator/",  # Joomla
        "/.env",
        "/.git/config",
        "/.git/HEAD",
        "/.aws/credentials",
        "/server-status",
        "/cgi-bin/",
        "/typo3/",
    }
)

_HONEYPOT_HIT_THRESHOLD = 3
_HONEYPOT_HIT_WINDOW_S = 60
_HONEYPOT_BLOCKLIST_S = 60 * 60  # 1h soft-blocklist on the IP


def _is_honeypot_path(path: str) -> bool:
    """Match `path` against the honeypot patterns. The `_HONEYPOT_PATHS`
    entries are either exact (e.g. `/wp-login.php`) or prefix
    (anything ending with `/`). We don't want substring matches — a
    legitimate `/admin/2fa/...` path must not trigger.
    """
    if not path:
        return False
    path = path or ""
    for pattern in _HONEYPOT_PATHS:
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif path == pattern:
            return True
    return False



def _record_hit(request: HttpRequest, path: str) -> None:
    """Write the audit row + bump the per-IP honeypot counter.

    Synchronous + DB-bound; the middleware wraps the call in
    `sync_to_async` to avoid blocking the event loop on a slow audit
    write. The recorder itself is best-effort.
    """
    ip = client_ip(request)
    user_agent = request.headers.get("User-Agent", "")
    request_id = getattr(request, "request_id", "") or ""

    audit_hook.record(
        action="security.honeypot.hit",
        actor=None,
        actor_label="system:honeypot",
        target_type="path",
        target_id=path[:128],
        ip=ip,
        user_agent=user_agent,
        request_id=request_id,
    )

    if ip:
        record_failure(
            "honeypot-ip",
            ip,
            threshold=_HONEYPOT_HIT_THRESHOLD,
            window_s=_HONEYPOT_HIT_WINDOW_S,
            lockout_s=_HONEYPOT_BLOCKLIST_S,
        )


_arecord_hit = sync_to_async(_record_hit, thread_sensitive=True)


class HoneypotMiddleware:
    """Async-only. Catches probes to well-known scanner paths, writes
    an audit row, and 404s the request.

    Position in MIDDLEWARE: very high — after `RequestIdMiddleware` so
    the audit row carries `request_id`, but before everything that
    might do its own routing / DB work for these paths. The honeypot
    pre-empts the URL dispatcher entirely.
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
                "HoneypotMiddleware requires the async ASGI stack."
            )

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path or ""
        if _is_honeypot_path(path):
            log.warning(
                "honeypot hit: path=%s ip=%s ua=%s",
                path,
                client_ip(request),
                request.headers.get("User-Agent", "")[:120],
            )
            try:
                await _arecord_hit(request, path)
            except Exception:
                log.exception("honeypot: audit write failed")
            return HttpResponseNotFound("Not Found")
        return await self.get_response(request)


def is_ip_honeypot_blocked(ip: str | None) -> bool:
    """Operator helper — check whether an IP has tripped the honeypot
    blocklist threshold. The middleware doesn't currently use this
    signal to deny non-honeypot traffic from the same IP (too noisy
    for default behavior); operators can wrap their custom views with
    this check or use it to drive a dashboard alert.
    """
    if not ip:
        return False
    return not is_locked("honeypot-ip", ip).allowed


__all__ = [
    "HoneypotMiddleware",
    "is_ip_honeypot_blocked",
]
