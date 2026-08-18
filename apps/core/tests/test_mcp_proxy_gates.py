"""The MCP proxy's per-call gates must see every call — v2+ and batch.

Two holes, one cause: enforcement hangs off the parsed tool name.

- The sunset/deprecation gate fed the raw tool name to
  `registry.by_slug`, which matches on bare slug. v2+ tools are listed
  as `slug__v2` (`EndpointSpec.mcp_tool_name`), so every versioned tool
  name resolved to no spec and the gate silently skipped it: a sunset
  v2 endpoint kept answering over MCP while REST 410'd it.
- A JSON-RPC batch (top-level array) parsed to no tool name at all, so
  throttle, the sunset gate and the APICallLog write were ALL skipped —
  N tool calls forwarded upstream, none metered, gated or recorded.
  Current MCP (2025-06-18) removed batching, so no compliant client is
  hurt by refusing outright.

DB-free: the principal is faked past auth, the registry is a local stub,
and both fixed paths short-circuit before the upstream forward. On the
unfixed code these tests fail with the 503 `mcp_unavailable` envelope —
the proxy forwarded the call at a loopback port nothing listens on,
which is itself the proof the gates never fired.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.test import RequestFactory, override_settings
from django.utils import timezone
from pydantic import BaseModel

from apps.core.principal import Principal
from apps.core.registry import EndpointSpec, Registry
from apps.mcp_proxy import views


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _spec(slug: str, version: str, *, sunset: bool = False) -> EndpointSpec:
    return EndpointSpec(
        slug=slug,
        version=version,
        cls=object,  # type: ignore[arg-type] — never instantiated by the proxy
        input_model=_In,
        output_model=_Out,
        sunset_at=timezone.now() - timedelta(days=1) if sunset else None,
    )


class _User:
    pk = 1
    is_authenticated = True
    is_staff = False


class _Credential:
    """Non-APIKey, non-URL-token: lands in the throttle's typed bucket."""

    pk = 99


@pytest.fixture(autouse=True)
def gated_world(monkeypatch):
    """Fake auth, stub registry, isolated cache counters."""

    async def fake_resolve(request):
        return Principal(user=_User(), credential=_Credential(), credential_kind="api_key")

    monkeypatch.setattr(views, "_resolve_principal", fake_resolve)

    stub = Registry()
    stub.register(_spec("get-note", "v1"))
    stub.register(_spec("get-note", "v2", sunset=True))
    stub.register(_spec("old-tool", "v1", sunset=True))
    stub.register(_spec("fresh-only", "v2"))
    monkeypatch.setattr(views, "registry", stub)

    override = override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "mcp-proxy-gate-tests",
            }
        }
    )
    with override:
        from django.core.cache import cache

        cache.clear()
        yield
        cache.clear()


def _call(body: str):
    request = RequestFactory().post("/mcp/", data=body, content_type="application/json")
    return async_to_sync(views.mcp_proxy_view)(request)


def _tools_call(name: str) -> str:
    return (
        '{"jsonrpc": "2.0", "id": 7, "method": "tools/call", '
        f'"params": {{"name": "{name}", "arguments": {{}}}}}}'
    )


# ---- the v2+ hole ---------------------------------------------------------


def test_a_sunset_v2_tool_is_refused_not_forwarded():
    response = _call(_tools_call("get-note__v2"))
    assert response.status_code == 200  # JSON-RPC error rides a 200
    content = response.content.decode()
    assert "endpoint_sunset" in content
    assert "mcp_unavailable" not in content


def test_a_sunset_v1_tool_stays_refused():
    """The bare-name path worked before the fix; keep it working."""
    response = _call(_tools_call("old-tool"))
    assert "endpoint_sunset" in response.content.decode()


def test_resolution_is_exact_not_highest_version():
    """`get-note` (bare) is the v1 registration — alive. Resolving it
    against the *highest* version would wrongly refuse it because v2 is
    sunset. The bridge registered the bare name from v1; gate likewise."""
    spec = views._spec_for_tool_name("get-note")
    assert spec is not None and spec.version == "v1"
    v2 = views._spec_for_tool_name("get-note__v2")
    assert v2 is not None and v2.version == "v2"
    # Only v2 exists → there is no bare tool; the registry must say so.
    assert views._spec_for_tool_name("fresh-only") is None
    assert views._spec_for_tool_name("fresh-only__v2") is not None
    assert views._spec_for_tool_name("never-heard-of-it") is None


# ---- the batch hole -------------------------------------------------------


def test_a_jsonrpc_batch_is_refused_not_forwarded():
    body = "[" + _tools_call("get-note") + "," + _tools_call("get-note") + "]"
    response = _call(body)
    assert response.status_code == 400
    content = response.content.decode()
    assert "batch_not_supported" in content
    assert "mcp_unavailable" not in content


def test_an_empty_batch_is_also_refused():
    response = _call("[]")
    assert response.status_code == 400
