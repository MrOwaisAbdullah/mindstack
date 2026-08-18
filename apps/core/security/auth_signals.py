"""Auth-signal subscribers + admin-login throttle middleware.

Two pieces wired together:

1. **`AdminLoginThrottleMiddleware`** — async middleware that intercepts
   POSTs to `<DJANGO_ADMIN_URL_PATH>login/` (and the custom-admin
   equivalent in Phase 10.2). Before Django's auth runs, checks the
   `admin-login` lockout sentinel for the source IP — if it's set,
   answers 429 + `Retry-After` and skips `get_response` entirely so
   the brute-forcer can't even attempt.

2. **`record_admin_login_failure(sender, credentials, request, **kwargs)`** —
   subscriber on `django.contrib.auth.signals.user_login_failed`. Bumps
   the `admin-login` fail counter ONLY when the request path matches
   one of the admin login URLs; non-admin login failures (allauth, our
   own magic-link, future API surfaces) don't share this counter
   because they have their own brute-force defenses.

Threshold per Phase 9.2.4 spec: 5 fails / 5min → 15min lockout per IP.

Wired in `apps.core.apps.CoreConfig.ready()` so the signal is connected
at app boot.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.contrib.auth.signals import user_login_failed
from django.dispatch import receiver
from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.core.security.alerts import notify_staff
from apps.core.security.client_ip import client_ip
from apps.core.security.lockout import is_locked, record_failure

log = logging.getLogger(__name__)


_ADMIN_LOGIN_THRESHOLD = 5
_ADMIN_LOGIN_WINDOW_S = 5 * 60
_ADMIN_LOGIN_LOCKOUT_S = 15 * 60


def _admin_login_paths() -> list[str]:
    """Compute the configured admin login paths (Django admin + custom).

    Returns paths like `["/admin/login/", "/_django-admin-CHANGE-ME/login/"]`.
    Empty / unset prefixes are skipped. Both are normalized with leading
    slash + trailing `login/`.
    """
    paths: list[str] = []
    for raw in (
        getattr(settings, "DJANGO_ADMIN_URL_PATH", ""),
        getattr(settings, "ADMIN_PANEL_URL_PATH", ""),
    ):
        if not raw:
            continue
        prefix = "/" + raw.strip("/") + "/"
        paths.append(prefix + "login/")
    return paths


def _is_admin_login_request(request: HttpRequest) -> bool:
    path = request.path or ""
    return any(path.startswith(p) or path == p for p in _admin_login_paths())


class AdminLoginThrottleMiddleware:
    """Async-only. Refuses POST attempts during a per-IP admin lockout.

    Position in MIDDLEWARE: any time after `RequestIdMiddleware` so a
    blocked request still gets a request_id echoed; before
    `AuthenticationMiddleware` so the lockout fires before Django
    materializes a User from the (untrusted) credential cookie.
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
                "AdminLoginThrottleMiddleware requires the async ASGI stack."
            )

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method == "POST" and _is_admin_login_request(request):
            ip = client_ip(request)
            if ip:
                gate = is_locked("admin-login", ip)
                if not gate.allowed:
                    log.warning(
                        "admin-login throttle: ip=%s blocked retry_after=%ds path=%s",
                        ip,
                        gate.retry_after_s,
                        request.path,
                    )
                    resp = JsonResponse(
                        {
                            "error": {
                                "code": "rate_limit_exceeded",
                                "message": (
                                    "Too many failed admin login attempts from this IP. "
                                    f"Retry after {gate.retry_after_s}s."
                                ),
                                "retry_after_s": gate.retry_after_s,
                            }
                        },
                        status=429,
                    )
                    resp.headers["Retry-After"] = str(gate.retry_after_s)
                    return resp
        return await self.get_response(request)


@receiver(user_login_failed)
def record_admin_login_failure(
    sender: object,
    credentials: dict | None = None,
    request: HttpRequest | None = None,
    **_kwargs: object,
) -> None:
    """Bump the admin-login fail counter on a failed login from an
    admin URL. Other login paths (allauth, magic-link consume) have
    their own throttles and don't poison this counter.

    Defensive: if `request` is None (some test harnesses fire the
    signal without one) we silently skip.
    """
    if request is None:
        return
    if not _is_admin_login_request(request):
        return
    ip = client_ip(request)
    if not ip:
        return
    result = record_failure(
        "admin-login",
        ip,
        threshold=_ADMIN_LOGIN_THRESHOLD,
        window_s=_ADMIN_LOGIN_WINDOW_S,
        lockout_s=_ADMIN_LOGIN_LOCKOUT_S,
    )
    # sentinel just tripped → notify the staff list. The
    # alert email is best-effort (fail_silently=True) so a dead SMTP
    # endpoint never breaks the failed-login path.
    if not result.allowed:
        notify_staff(
            subject="Admin login lockout triggered",
            body=(
                f"Admin login from {ip} crossed the {_ADMIN_LOGIN_THRESHOLD}-fail "
                f"threshold within {_ADMIN_LOGIN_WINDOW_S}s. Source IP locked out for "
                f"{_ADMIN_LOGIN_LOCKOUT_S}s. Path: {request.path}. "
                f"User-Agent: {request.headers.get('User-Agent', '')[:200]}."
            ),
        )


__all__ = [
    "AdminLoginThrottleMiddleware",
    "record_admin_login_failure",
]
