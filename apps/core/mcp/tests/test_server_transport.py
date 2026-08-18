"""The FastMCP subprocess must run the http transport with JSON replies.

Without `json_response=True`, FastMCP frames every tool result as a
`text/event-stream` SSE response. Those leave the Django proxy as chunked
responses with no Content-Length, which edge proxies (Cloudflare et al.)
buffer and cap (~64 KiB) — a large tool result truncates mid-JSON and a
strict MCP client hangs waiting for the rest of the message. This test
pins the transport configuration so a refactor of `main()` can't silently
regress it.
"""
from __future__ import annotations

from typing import Any

import pytest

from fastmcp import FastMCP


def test_main_runs_http_transport_with_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(
        self: FastMCP,
        transport: str | None = None,
        show_banner: bool | None = None,
        **transport_kwargs: Any,
    ) -> None:
        captured["transport"] = transport
        captured.update(transport_kwargs)

    monkeypatch.setattr(FastMCP, "run", fake_run)

    from apps.core.mcp import server

    server.main()

    assert captured["transport"] == "http"
    assert captured["json_response"] is True
