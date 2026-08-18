"""Maintenance-mode middleware.

When `apps.core.maintenance.is_enabled()` returns True, every request
is short-circuited to a 503 UNLESS:

- the request path is on the bypass list (health checks, static, and
  every door the operator needs to get back in — see `_is_bypass_path`),
- OR the resolved user is `is_staff` (the operator keeps browsing the
  whole site, so they can verify whatever the window was for).

Returns `Retry-After: 60` and `Cache-Control: no-store` either way. The
body depends on who asked: a branded HTML page for a browser, and the
project's JSON error envelope under `/api/` and `/mcp/`, because a
consumer that parses every other error as JSON should not get an HTML
blob for this one.

Position in MIDDLEWARE: AFTER `AuthenticationMiddleware`, since
`request.user` is the bypass condition, and before the request reaches
a view.

**Getting back out.** The bypass list is the whole safety story here. A
maintenance flag that locks the operator out of the page that turns it
off is worse than no maintenance flag, so the list is derived from
settings rather than hardcoded: `ADMIN_PANEL_URL_PATH`,
`DJANGO_ADMIN_URL_PATH`, `LOGIN_URL`, logout, and `/setup/`. The
original list had a hardcoded `/auth/` inherited from the template,
which is not a route in this project — the login page is at `LOGIN_URL`
— so a logged-out operator who turned maintenance on could not have
signed back in to turn it off.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.loader import render_to_string

from apps.core import maintenance

log = logging.getLogger(__name__)

_BYPASS_PREFIXES = (
    "/healthz",
    "/readyz",
    "/static/",
    "/media/",
    "/.well-known/",
    # The operator's own doors. `/setup/` matters because an install can
    # be mid-setup when the flag is flipped, and the setup middleware
    # would otherwise bounce them into a 503.
    "/logout/",
    "/setup/",
)

# Surfaces whose entire error contract is JSON. `apps.core.rest` and the
# MCP proxy both answer `{"error": {"code", "message"}}` for every other
# failure mode.
_JSON_PREFIXES = ("/api/", "/mcp/", "/mcp")


def _settings_prefix(name: str) -> str:
    """`/<value>/` for a settings key holding a bare URL path segment."""
    raw = getattr(settings, name, "") or ""
    return ("/" + str(raw).strip("/") + "/") if raw else ""


def _login_prefix() -> str:
    raw = str(getattr(settings, "LOGIN_URL", "") or "")
    if not raw or not raw.startswith("/"):
        return ""
    return raw if raw.endswith("/") else raw + "/"


def _is_bypass_path(path: str) -> bool:
    if any(path.startswith(p) for p in _BYPASS_PREFIXES):
        return True
    for prefix in (
        _settings_prefix("ADMIN_PANEL_URL_PATH"),
        _settings_prefix("DJANGO_ADMIN_URL_PATH"),
        _login_prefix(),
    ):
        if prefix and path.startswith(prefix):
            return True
    return False


def _wants_json(path: str) -> bool:
    return path.startswith(_JSON_PREFIXES)


def _user_is_staff(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and getattr(user, "is_staff", False))


def _stamp(response: HttpResponse) -> HttpResponse:
    response["Retry-After"] = "60"
    response["Cache-Control"] = "no-store"
    return response


def _render_503(request: HttpRequest) -> HttpResponse:
    message = maintenance.get_message()

    if _wants_json(request.path or ""):
        # Matches the envelope every other error on these surfaces uses.
        return _stamp(
            JsonResponse(
                {"error": {"code": "maintenance", "message": message}},
                status=503,
            )
        )

    # Local import: apps/core is the vendored framework and must not gain a
    # module-level dependency on a brain app.
    from apps.brainconfig import services as brainconfig_services

    try:
        body = render_to_string(
            "errors/maintenance.html",
            {
                "APP_NAME": brainconfig_services.app_name(),
                "maintenance_message": message,
            },
            request=request,
        )
    except Exception:
        # The maintenance window may be *for* whatever broke the template
        # loader or the settings table. Answering 503 is the job; the
        # branding is not.
        log.exception("maintenance: 503 template render failed")
        return _stamp(
            HttpResponse(
                message, status=503, content_type="text/plain; charset=utf-8"
            )
        )
    return _stamp(
        HttpResponse(body, status=503, content_type="text/html; charset=utf-8")
    )


class MaintenanceModeMiddleware:
    """Async-only. Returns 503 + branded page when the flag is on."""

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
                "MaintenanceModeMiddleware requires the async ASGI stack."
            )

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        if not await sync_to_async(maintenance.is_enabled, thread_sensitive=True)():
            return await self.get_response(request)
        if _is_bypass_path(request.path or ""):
            return await self.get_response(request)
        # `request.user` is a SimpleLazyObject that hits the session DB
        # on first access — must run in a sync thread.
        is_staff = await sync_to_async(_user_is_staff, thread_sensitive=True)(request)
        if is_staff:
            return await self.get_response(request)
        return await sync_to_async(_render_503, thread_sensitive=True)(request)


__all__ = ["MaintenanceModeMiddleware"]
