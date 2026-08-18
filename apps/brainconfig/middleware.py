"""Send an unconfigured server to its setup wizard.

Two distinct situations, deliberately handled differently:

- **No operator account exists.** The app is unowned: nobody can log in,
  nothing can be configured, and the only useful page is the wizard. Every
  human-facing route redirects there.
- **An account exists but setup is unfinished.** Only the ops UI
  redirects, and only for the signed-in operator. The rest of the app
  keeps its normal behaviour — a half-configured server should return its
  usual errors to API clients rather than 302 them into an HTML wizard.

Machine surfaces are never redirected. An API or MCP caller that gets a
302 to `/setup/` sees a confusing HTML body instead of the JSON error
that tells it what is actually wrong, and health checks that follow
redirects would report a broken server as healthy.
"""
from __future__ import annotations

import logging

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.conf import settings
from django.shortcuts import redirect

log = logging.getLogger(__name__)

#: Latched so an open window costs one log line per process, not one per
#: request. An unclaimed server can sit for days; a warning that scrolls
#: is a warning nobody reads.
_open_window_warned = False


def reset_open_window_warning() -> None:
    """Test hook — the latch is module state and outlives a test case."""
    global _open_window_warned
    _open_window_warned = False


def _warn_open_window() -> None:
    """Say, once, that anyone who can reach this box can claim it.

    SECURITY.md promises this warning. It is emitted here rather than from
    `AppConfig.ready()` because `ready()` runs before the database is
    reliably available, and querying there is a well-known way to build an
    app that cannot start. The middleware already asks the question on the
    way through, so the answer costs nothing.
    """
    global _open_window_warned
    if _open_window_warned:
        return
    _open_window_warned = True
    log.warning(
        "SETUP IS UNCLAIMED: no operator account exists, so anyone who can "
        "reach this server can create the first one and take ownership of "
        "the ops UI — every private note and every stored credential. "
        "Create your account now, before this box is publicly reachable. "
        "This warning stops once the account exists."
    )

#: Prefixes that must answer normally even with nothing configured.
_EXEMPT_PREFIXES = (
    "/setup/",
    "/healthz",
    "/readyz",
    "/static/",
    "/media/",
    "/api/",
    "/mcp",
    "/webhooks/",
    "/_csp-report/",
    "/.well-known/",
    "/robots.txt",
    "/login/",
    "/logout/",
)


class SetupRequiredMiddleware:
    """Redirect to `/setup/` while the server is unusable or unfinished.

    Async-only, like every neighbour in MIDDLEWARE. This and WhiteNoise
    were the last two sync-only middlewares in the chain, and Django's
    per-middleware adaptation therefore wrapped the whole inner stack in
    `async_to_sync` — four boundary crossings and two pinned threadpool
    threads on every request. The exempt prefixes (which include `/api/`
    and `/mcp`, the long-running async surfaces) short-circuit on a pure
    path test with no DB read at all; only human-facing routes pay the
    one `sync_to_async` hop for the setup predicates.
    """

    sync_capable = False
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        markcoroutinefunction(self)
        if not iscoroutinefunction(get_response):
            raise RuntimeError(
                "SetupRequiredMiddleware requires the async ASGI stack."
            )
        self._ops_prefix = "/" + (settings.ADMIN_PANEL_URL_PATH or "ops/").strip("/") + "/"
        # The settings page is exempt from the unfinished-setup redirect.
        # Completion is DERIVED (see setup_state), so clearing a required
        # value — the ANTHROPIC_API_KEY "clear" checkbox on that very
        # page — used to flip `is_complete()` and eject the operator from
        # the whole ops UI, including the one page that edits stored
        # settings. Same rule as the maintenance-mode bypass list: the
        # switch you would use to undo a state must survive that state.
        # Everything else still routes to the wizard, which is built for
        # the incomplete case.
        self._settings_prefix = self._ops_prefix + "settings/"

    async def __call__(self, request):
        if any(request.path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await self.get_response(request)
        # The predicates and `request.user` (a SimpleLazyObject over the
        # session table) are both DB-bound — sync thread.
        target = await sync_to_async(self._redirect_target, thread_sensitive=True)(
            request
        )
        if target is not None:
            return target
        return await self.get_response(request)

    def _redirect_target(self, request):
        """The decision body, unchanged from the sync version: a redirect
        response, or None to pass through."""
        from apps.brainconfig import setup_state

        if setup_state.needs_first_admin():
            _warn_open_window()
            return redirect("setup:home")
        path = request.path
        if path.startswith(self._ops_prefix) and not path.startswith(
            self._settings_prefix
        ):
            user = getattr(request, "user", None)
            if (
                user is not None
                and user.is_authenticated
                and user.is_staff
                and not setup_state.is_complete()
            ):
                return redirect("setup:home")
        return None
