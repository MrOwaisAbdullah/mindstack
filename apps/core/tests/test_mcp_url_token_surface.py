"""The `/mcp/k/<token>/` surface: routed, and invisible until asked for.

This exists so a Claude.ai custom connector (web + mobile) can authenticate
at all — its dialog has nowhere to put an `Authorization` header, so the
credential has to ride in the path. That makes the URL itself a bearer
secret, which is why the default matters as much as the route does.

Two things are pinned here, both DB-free:

- **The route resolves**, in both slash forms, passing the raw token through
  as the view's `token` kwarg. Without the route the view's url-token branch
  is unreachable code and the connector gets a 404 that looks like the flag.
- **The flag is off by default, and off means 404** — not 401. A closed door
  that answers "wrong key" confirms the surface exists; this one denies that
  the address is real.

`apps.url_mcp_tokens` is vendored now, so with the flag on the route resolves
purpose-built `mcpurl_` tokens: prefix lockout on its own scope, scrubbed out
of the logs, expiring, revocable without touching your bearer keys. The
credential itself is covered by `test_url_token_credential.py`. Ordinary
`mcpsk_` keys still resolve here too — the bearer registry is shared — and
that path has none of those properties, which is why the connector should be
pointed at a `mcpurl_` token.

What is NOT covered: the flag-on path. It runs `resolve_bearer`, which hits
the credential tables, and these tests stay off the DB (the host venv has no
`django_redis`, so `django_db` cannot build a cache backend here).
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.test import RequestFactory, override_settings
from django.urls import resolve

from apps.mcp_proxy.views import mcp_proxy_view
from config.settings.env import Settings

TOKEN = "mcpsk_AbCdEfGh0123456789"


# ----- Routing ----------------------------------------------------------------


@pytest.mark.parametrize("path", [f"/mcp/k/{TOKEN}/", f"/mcp/k/{TOKEN}"])
def test_both_slash_forms_route_to_the_proxy_view(path: str) -> None:
    # MCP clients are inconsistent about the trailing slash and the plain
    # `/mcp` route already carries both forms for that reason.
    match = resolve(path)
    assert match.func is mcp_proxy_view
    assert match.kwargs == {"token": TOKEN}


def test_bare_mcp_route_is_untouched() -> None:
    # The header-authed surface every current client uses. If adding the
    # token pattern ever shadows it, that breaks Desktop, Code and Cursor
    # at once.
    match = resolve("/mcp/")
    assert match.func is mcp_proxy_view
    assert match.kwargs == {}


# ----- The flag ---------------------------------------------------------------


def test_url_auth_defaults_off_in_code() -> None:
    # The field default, NOT `settings.MCP_URL_AUTH_ENABLED` — that one
    # resolves against whatever `.env` the machine happens to carry, so
    # asserting on it fails for anyone who has legitimately turned the
    # surface on. What is worth pinning is the code-level decision: this
    # field defaults False *against* the PLAN2 default of True, because
    # on means a live credential in the access log.
    assert Settings.model_fields["MCP_URL_AUTH_ENABLED"].default is False


@override_settings(MCP_URL_AUTH_ENABLED=False)
def test_flag_off_returns_404_not_401() -> None:
    request = RequestFactory().post(
        f"/mcp/k/{TOKEN}/", data="{}", content_type="application/json"
    )
    response = async_to_sync(mcp_proxy_view)(request, token=TOKEN)
    assert response.status_code == 404


@override_settings(MCP_URL_AUTH_ENABLED=False)
def test_flag_off_does_not_leak_the_token_into_the_body() -> None:
    request = RequestFactory().post(
        f"/mcp/k/{TOKEN}/", data="{}", content_type="application/json"
    )
    response = async_to_sync(mcp_proxy_view)(request, token=TOKEN)
    assert TOKEN not in response.content.decode()
