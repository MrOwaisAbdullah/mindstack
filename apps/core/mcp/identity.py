"""Per-request identity / request-id contextvars for the MCP subprocess.

The Django-side proxy view (`apps/mcp_proxy/views.py`) is the only trusted
caller of the FastMCP subprocess. When it forwards a request, it sets these
headers on the loopback hop:

- `X-MCP-User-Id`        — pk of `accounts.User` (set from the authenticated
                            principal; `None` for anonymous callers)
- `X-MCP-Credential-Id`  — pk of `api_keys.APIKey` or `mcp_oauth.AccessToken`
                            (set from the authenticated principal; `None` for
                            anonymous callers)
- `X-MCP-Credential-Kind`— which of those `X-MCP-Credential-Id` is a pk *of*
                            ("api_key" | "oauth_token" | …). A pk is
                            meaningless without it: `APIKey` 3 and
                            `AccessToken` 3 are different credentials with
                            different tiers.
- `X-MCP-Request-Id`     — uuid4 hex; echoed across the trace boundary

The subprocess's loopback ASGI middleware (`apps/core/mcp/server.py`) reads
those headers, sets the contextvars below, and the bridge handler
(`apps/core/mcp/bridge.py`) reads them to populate `Ctx`.

Why contextvars and not function args: FastMCP's tool dispatch decides
arguments from the JSON-RPC payload alone. The identity envelope rides
out-of-band on HTTP headers, so we need a per-task store that survives the
hop from middleware → bridge handler.
"""
from __future__ import annotations

from contextvars import ContextVar

mcp_user_id_var: ContextVar[str | None] = ContextVar("mcp_user_id", default=None)
mcp_credential_var: ContextVar[str | None] = ContextVar("mcp_credential", default=None)
mcp_credential_kind_var: ContextVar[str | None] = ContextVar(
    "mcp_credential_kind", default=None
)
mcp_request_id_var: ContextVar[str | None] = ContextVar("mcp_request_id", default=None)
