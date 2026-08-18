"""T17 — verify the FastMCP subprocess loopback gate.

Covers both trust paths in :class:`apps.core.mcp.server.LoopbackIdentityMiddleware`:

- **Shared-secret path** (when ``MCP_LOOPBACK_SECRET`` is set): only callers
  presenting a matching ``X-MCP-Loopback-Secret`` header pass; any other peer IP
  is irrelevant. Constant-time compare via :func:`hmac.compare_digest`.
- **Loopback-IP path** (when no secret is configured): only requests whose peer
  IP is in ``{127.0.0.1, ::1, localhost}`` pass; any other peer IP returns 403
  before the identity headers are read.

Also confirms the middleware strips ``X-MCP-User-Id`` / ``X-MCP-Credential-Id`` /
``X-MCP-Request-Id`` from request headers onto contextvars, which is the entire
point of the loopback hop — the Django proxy is the only trusted caller.
"""
from __future__ import annotations

import pytest
from django.test import override_settings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apps.core.mcp.identity import (
    mcp_credential_var,
    mcp_request_id_var,
    mcp_user_id_var,
)
from apps.core.mcp.server import LoopbackIdentityMiddleware


async def _echo_identity(request):  # noqa: ANN001
    return PlainTextResponse(
        f"user={mcp_user_id_var.get()};cred={mcp_credential_var.get()};rid={mcp_request_id_var.get()}",
        status_code=200,
    )


def _build_app() -> Starlette:
    return Starlette(
        routes=[Route("/probe", _echo_identity, methods=["GET"])],
        middleware=[Middleware(LoopbackIdentityMiddleware)],
    )


# ----- Shared-secret trust path -----------------------------------------------


@override_settings(MCP_LOOPBACK_SECRET="testsecret-abc")
def test_secret_path_rejects_missing_header() -> None:
    client = TestClient(_build_app(), client=("203.0.113.10", 1234))
    r = client.get("/probe")
    assert r.status_code == 403
    assert "X-MCP-Loopback-Secret" in r.text


@override_settings(MCP_LOOPBACK_SECRET="testsecret-abc")
def test_secret_path_rejects_wrong_secret() -> None:
    client = TestClient(_build_app(), client=("203.0.113.10", 1234))
    r = client.get("/probe", headers={"X-MCP-Loopback-Secret": "wrong"})
    assert r.status_code == 403


@override_settings(MCP_LOOPBACK_SECRET="testsecret-abc")
def test_secret_path_accepts_correct_secret_regardless_of_peer_ip() -> None:
    # Bridge IP from a Docker container — would fail the loopback-IP gate, but
    # the correct shared secret unlocks the call.
    client = TestClient(_build_app(), client=("172.18.0.3", 1234))
    r = client.get(
        "/probe",
        headers={
            "X-MCP-Loopback-Secret": "testsecret-abc",
            "X-MCP-User-Id": "42",
            "X-MCP-Credential-Id": "9",
            "X-MCP-Request-Id": "req-xyz",
        },
    )
    assert r.status_code == 200
    assert "user=42;cred=9;rid=req-xyz" == r.text


# ----- Loopback-IP trust path (no secret configured) --------------------------


@override_settings(MCP_LOOPBACK_SECRET="")
def test_loopback_path_rejects_non_loopback_peer() -> None:
    # 203.0.113.x is RFC 5737 TEST-NET-3, a stand-in for an external attacker.
    client = TestClient(_build_app(), client=("203.0.113.10", 1234))
    r = client.get("/probe")
    assert r.status_code == 403
    assert "loopback" in r.text.lower()


@override_settings(MCP_LOOPBACK_SECRET="")
def test_loopback_path_rejects_docker_bridge_peer() -> None:
    # A Docker bridge IP — the explicit deployment case where operators are
    # expected to set MCP_LOOPBACK_SECRET. Without it, the gate fails closed.
    client = TestClient(_build_app(), client=("172.18.0.3", 1234))
    r = client.get("/probe")
    assert r.status_code == 403


@override_settings(MCP_LOOPBACK_SECRET="")
@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "localhost"])
def test_loopback_path_accepts_loopback_peers(peer: str) -> None:
    client = TestClient(_build_app(), client=(peer, 1234))
    r = client.get(
        "/probe",
        headers={
            "X-MCP-User-Id": "7",
            "X-MCP-Credential-Id": "3",
            "X-MCP-Request-Id": "req-1",
        },
    )
    assert r.status_code == 200
    assert r.text == "user=7;cred=3;rid=req-1"


# ----- Identity-handoff invariants --------------------------------------------


@override_settings(MCP_LOOPBACK_SECRET="")
def test_identity_contextvars_default_to_none_when_headers_absent() -> None:
    client = TestClient(_build_app(), client=("127.0.0.1", 1234))
    r = client.get("/probe")
    assert r.status_code == 200
    assert r.text == "user=None;cred=None;rid=None"


@override_settings(MCP_LOOPBACK_SECRET="")
def test_identity_contextvars_reset_after_request() -> None:
    # After a request that pinned identity, fresh contextvars should default
    # back to None — the middleware uses try/finally to reset.
    client = TestClient(_build_app(), client=("127.0.0.1", 1234))
    client.get(
        "/probe",
        headers={"X-MCP-User-Id": "99", "X-MCP-Credential-Id": "1", "X-MCP-Request-Id": "rid"},
    )
    assert mcp_user_id_var.get() is None
    assert mcp_credential_var.get() is None
    assert mcp_request_id_var.get() is None
