"""T21 — defense-in-depth sunset gate inside the MCP bridge handler.

The Django proxy short-circuits `tools/call` to a sunset endpoint with a
JSON-RPC tool error envelope BEFORE forwarding to the loopback subprocess
(verified in :mod:`apps.mcp_proxy.tests.test_deprecation_proxy`). The
bridge handler then has its OWN sunset check ([apps/core/mcp/bridge.py:87-93])
so a loopback-direct caller (test harness, internal management command,
operator running the FastMCP subprocess by hand) can never reach
``spec.cls().run(...)`` after sunset. This test asserts that defense-in-
depth raise actually fires.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from pydantic import BaseModel

from apps.core.mcp.bridge import build_handler
from apps.core.registry import Endpoint, endpoint, registry


@pytest.fixture
def _isolated_registry():
    saved = dict(registry._specs)
    yield
    registry._specs.clear()
    registry._specs.update(saved)


def _make_spec(*, slug, deprecated_at=None, sunset_at=None, deprecation_message=""):
    class _I(BaseModel):
        name: str = "world"

    class _O(BaseModel):
        greeting: str

    @endpoint(
        slug=slug,
        description=f"Test {slug}.",
        deprecated_at=deprecated_at,
        sunset_at=sunset_at,
        deprecation_message=deprecation_message,
    )
    class _E(Endpoint):
        Input = _I
        Output = _O

        async def run(self, inp, ctx):  # type: ignore[override]
            return self.Output(greeting=f"Hi, {inp.name}")

    return _E.__endpoint_spec__  # type: ignore[attr-defined]


def test_bridge_handler_raises_after_sunset(_isolated_registry) -> None:
    """A loopback-direct call to a past-sunset tool must NOT reach run() —
    the bridge handler short-circuits with the deprecation_message text.
    """
    import asyncio

    past = timezone.now() - timedelta(days=1)
    spec = _make_spec(
        slug="bridge-sunset-defense",
        deprecated_at=past - timedelta(days=30),
        sunset_at=past,
        deprecation_message="Pick v2 — this tool retired on 2026-05-24.",
    )

    handler = build_handler(spec)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(handler(name="anything"))
    assert "Pick v2" in str(exc.value)


def test_bridge_handler_runs_before_sunset(_isolated_registry) -> None:
    """Sanity: not-yet-sunset endpoints still execute through the bridge.

    Without this companion test the previous assertion could pass on a
    bridge that's accidentally broken to raise on every call.
    """
    import asyncio

    future = timezone.now() + timedelta(days=30)
    spec = _make_spec(
        slug="bridge-not-yet-sunset",
        deprecated_at=timezone.now() - timedelta(days=1),
        sunset_at=future,
        deprecation_message="Migrate to v2 before sunset.",
    )

    handler = build_handler(spec)
    result = asyncio.run(handler(name="alice"))
    assert result == {"greeting": "Hi, alice"}
