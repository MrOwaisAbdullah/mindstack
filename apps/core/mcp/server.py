"""FastMCP subprocess entrypoint.

Runs as a separate process from Django: `python -m apps.core.mcp.server`.
The Django proxy view at `/mcp/` (`apps/mcp_proxy/views.py`) forwards every
MCP request to this subprocess on `MCP_LOOPBACK_HOST:MCP_LOOPBACK_PORT`.

Trust model
-----------
The subprocess accepts requests *only* from `127.0.0.1` / `::1`. The
`X-MCP-User-Id` / `X-MCP-Credential-Id` / `X-MCP-Credential-Kind` /
`X-MCP-Request-Id` headers carry
the resolved Principal across the hop. Because Django is the only loopback
caller (production hardening pins the listener to localhost), those headers
cannot be forged externally — anything not from loopback is 403'd before
the headers are read.

Boot sequence
-------------
1. Django setup so `apps.core.apps.CoreConfig.ready()` runs and triggers
   `autodiscover_modules("endpoints")`. This populates the registry.
2. Build a FastMCP instance and register every spec via the bridge.
3. Wrap with the loopback + identity middleware.
4. Start the streamable-HTTP transport on the configured loopback port.
"""
from __future__ import annotations

import hmac
import logging
import os

import django
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from apps.core.mcp.identity import (
    mcp_credential_kind_var,
    mcp_credential_var,
    mcp_request_id_var,
    mcp_user_id_var,
)

log = logging.getLogger(__name__)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class LoopbackIdentityMiddleware(BaseHTTPMiddleware):
    """Trust check + identity handoff.

    Two trust paths, in order:

    1. **Shared-secret header** — when `MCP_LOOPBACK_SECRET` is configured,
       accept callers presenting a matching `X-MCP-Loopback-Secret` regardless
       of peer IP. Required for Docker compose / k8s deployments where Django
       and MCP run in separate network namespaces and the connection arrives
       from a bridge IP rather than loopback.
    2. **Loopback IP** (legacy fallback) — when no secret is configured, fall
       back to a strict peer-IP check (127.0.0.1 / ::1 / localhost). This keeps
       the host-only dev path (`make dev` + `make mcp` on the same machine)
       working without any env wiring.

    Either path also strips the `X-MCP-*` identity headers off the request and
    pins them onto contextvars for downstream tool handlers to read.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        from django.conf import settings  # deferred — settings configured at boot

        secret = getattr(settings, "MCP_LOOPBACK_SECRET", "") or ""
        if secret:
            presented = request.headers.get("x-mcp-loopback-secret", "")
            if not hmac.compare_digest(presented, secret):
                log.warning(
                    "mcp subprocess: rejecting request with missing/invalid X-MCP-Loopback-Secret path=%s",
                    request.url.path,
                )
                return PlainTextResponse(
                    "MCP subprocess: X-MCP-Loopback-Secret missing or invalid.",
                    status_code=403,
                )
        else:
            client_host = request.client.host if request.client else None
            if client_host not in _LOOPBACK_HOSTS:
                log.warning(
                    "mcp subprocess: rejecting non-loopback peer %r path=%s",
                    client_host,
                    request.url.path,
                )
                return PlainTextResponse(
                    "MCP subprocess accepts loopback requests only.", status_code=403
                )

        token_user = mcp_user_id_var.set(request.headers.get("x-mcp-user-id"))
        token_cred = mcp_credential_var.set(request.headers.get("x-mcp-credential-id"))
        token_kind = mcp_credential_kind_var.set(
            request.headers.get("x-mcp-credential-kind")
        )
        token_rid = mcp_request_id_var.set(request.headers.get("x-mcp-request-id"))
        try:
            return await call_next(request)
        finally:
            mcp_user_id_var.reset(token_user)
            mcp_credential_var.reset(token_cred)
            mcp_credential_kind_var.reset(token_kind)
            mcp_request_id_var.reset(token_rid)


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    django.setup()

    from django.conf import settings

    from apps.core.mcp.bridge import register_all
    from fastmcp import FastMCP

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Read once, at subprocess boot — FastMCP fixes the name for the life
    # of the server, so a rename on /ops/settings/ lands on the next
    # `reload mcp` / container restart.
    from apps.brainconfig.services import app_name

    mcp = FastMCP(app_name() or "MCP Server")
    count = register_all(mcp)
    log.info("mcp subprocess: registered %d tool(s)", count)

    host = settings.MCP_LOOPBACK_HOST
    port = int(settings.MCP_LOOPBACK_PORT)
    log.info("mcp subprocess: listening on http://%s:%d/mcp/", host, port)

    mcp.run(
        transport="http",
        host=host,
        port=port,
        middleware=[Middleware(LoopbackIdentityMiddleware)],
        show_banner=False,
        # Single-JSON replies instead of FastMCP's default per-result SSE
        # framing. SSE replies leave the Django proxy as chunked responses
        # with no Content-Length, and edge proxies (Cloudflare et al.) cap
        # those at ~64 KiB — a large tool result truncates mid-JSON and a
        # strict MCP client waits forever for the rest. JSON replies get a
        # Content-Length and pass through intact.
        json_response=True,
    )


if __name__ == "__main__":
    main()
