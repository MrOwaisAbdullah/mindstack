"""The MCP caller's identity must survive the loopback hop.

Written because it did not. `bridge.build_handler` built every `Ctx`
with `user=None, credential=None` and a `# Phase 3 resolves…` comment,
so the identity the Django proxy had already authenticated was thrown
away on arrival. `tiers.tier_for_credential(None)` returns `public`, so
the visible symptom was an agents-only key reading two public notes out
of a seven-note brain over MCP while returning all seven over REST, and
`propose-feed` refusing every caller alive. It failed *closed*, which is
why it survived so long: nothing leaked, an agent just reported an empty
brain.

Three links in the chain, one test each, plus the end-to-end:

1. the proxy stamps `X-MCP-Credential-Kind` alongside the id — a bare pk
   is ambiguous across credential types (see `test_proxy_forwards_*`,
   in `apps.mcp_proxy.tests`),
2. the loopback middleware pins it to a contextvar,
3. the bridge rehydrates `(kind, pk)` back into rows and puts them on
   the `Ctx`.

Deliberately DB-free: a fake credential kind is registered against the
bearer rehydrator registry, so these run without `django_db` (the host
venv has no `django_redis`, so DB-marked tests can't set up a cache
backend there — see CLAUDE.md).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from django.test import override_settings
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apps.core import bearer
from apps.core.mcp.bridge import build_handler
from apps.core.mcp.identity import (
    mcp_credential_kind_var,
    mcp_credential_var,
    mcp_user_id_var,
)
from apps.core.mcp.server import LoopbackIdentityMiddleware
from apps.core.registry import Endpoint, endpoint, registry


@dataclass
class _FakeUser:
    pk: int
    is_active: bool = True
    is_authenticated: bool = True


@dataclass
class _FakeCredential:
    pk: int
    user_id: int
    user: _FakeUser


_KIND = "test-cred-kind"
_USER = _FakeUser(pk=4242)
_CRED = _FakeCredential(pk=77, user_id=4242, user=_USER)


@pytest.fixture
def _fake_rehydrator():
    """Register a credential kind that resolves without touching the DB."""
    saved = dict(bearer._rehydrators)
    bearer.register_rehydrator(_KIND, lambda pk: _CRED if str(pk) == "77" else None)
    yield
    bearer._rehydrators.clear()
    bearer._rehydrators.update(saved)


@pytest.fixture
def _identity_headers():
    """Set the contextvars the loopback middleware would have set, and
    always put them back — a leaked contextvar would silently hand the
    next test someone else's credential."""
    tokens = []

    def _set(*, user_id, credential_id, kind):
        tokens.append((mcp_user_id_var, mcp_user_id_var.set(user_id)))
        tokens.append((mcp_credential_var, mcp_credential_var.set(credential_id)))
        tokens.append((mcp_credential_kind_var, mcp_credential_kind_var.set(kind)))

    yield _set
    for var, token in reversed(tokens):
        var.reset(token)


@pytest.fixture
def _isolated_registry():
    saved = dict(registry._specs)
    yield
    registry._specs.clear()
    registry._specs.update(saved)


def _identity_echo_spec(slug: str):
    """An endpoint that reports what identity landed on its Ctx."""

    class _I(BaseModel):
        pass

    class _O(BaseModel):
        user_pk: str
        credential_pk: str

    @endpoint(slug=slug, description="Echo the resolved identity.")
    class _E(Endpoint):
        Input = _I
        Output = _O

        async def run(self, inp, ctx):  # type: ignore[override]
            return self.Output(
                user_pk=str(getattr(ctx.user, "pk", "")),
                credential_pk=str(getattr(ctx.credential, "pk", "")),
            )

    return _E.__endpoint_spec__  # type: ignore[attr-defined]


# ----- the bridge: (kind, pk) -> rows on the Ctx --------------------------------


def test_bridge_puts_the_named_credential_on_the_ctx(
    _fake_rehydrator, _identity_headers, _isolated_registry
) -> None:
    """The regression. An endpoint invoked through the bridge must see the
    credential the headers named — this is what makes an MCP caller read at
    its key's tier instead of `public`."""
    _identity_headers(user_id="4242", credential_id="77", kind=_KIND)

    handler = build_handler(_identity_echo_spec("bridge-identity-echo"))
    result = asyncio.run(handler())

    assert result == {"user_pk": "4242", "credential_pk": "77"}


def test_bridge_resolves_user_without_a_second_query(
    _fake_rehydrator, _identity_headers, _isolated_registry
) -> None:
    """When the header user matches the credential's owner, the user comes
    off the already-joined credential — the same object, not a reload."""
    _identity_headers(user_id="4242", credential_id="77", kind=_KIND)

    handler = build_handler(_identity_echo_spec("bridge-identity-join"))
    asyncio.run(handler())

    from apps.core.mcp.bridge import _identity_from_headers

    user, credential = asyncio.run(_identity_from_headers())
    assert user is _USER
    assert credential is _CRED


def test_unknown_credential_kind_falls_back_to_no_credential(
    _identity_headers, _isolated_registry
) -> None:
    """No rehydrator registered for the kind → no credential, no crash.

    Fail closed: an unrecognised kind must not resolve to something we
    would then grant a tier to. `public` is the correct answer here.
    """
    _identity_headers(user_id="4242", credential_id="77", kind="a-kind-nobody-registered")

    from apps.core.mcp.bridge import _credential_from_headers

    assert asyncio.run(_credential_from_headers()) is None


def test_dead_credential_falls_back_to_no_credential(
    _fake_rehydrator, _identity_headers, _isolated_registry
) -> None:
    """The rehydrator re-applies its liveness guards, so a revoked/expired
    row resolves to None rather than to a live credential."""
    _identity_headers(user_id="4242", credential_id="999", kind=_KIND)

    from apps.core.mcp.bridge import _credential_from_headers

    assert asyncio.run(_credential_from_headers()) is None


def test_anonymous_caller_gets_no_identity(_identity_headers, _isolated_registry) -> None:
    """Absent headers must stay `(None, None)` — the pre-fix behaviour was
    only ever *wrong*, not unsafe, and the unauthenticated case still has
    to land there."""
    _identity_headers(user_id=None, credential_id=None, kind=None)

    from apps.core.mcp.bridge import _identity_from_headers

    user, credential = asyncio.run(_identity_from_headers())
    assert user is None
    assert credential is None


# ----- the loopback middleware: header -> contextvar ----------------------------


async def _echo_kind(request):  # noqa: ANN001
    return PlainTextResponse(f"kind={mcp_credential_kind_var.get()}", status_code=200)


@override_settings(MCP_LOOPBACK_SECRET="")
def test_middleware_pins_credential_kind_to_a_contextvar() -> None:
    app = Starlette(
        routes=[Route("/probe", _echo_kind, methods=["GET"])],
        middleware=[Middleware(LoopbackIdentityMiddleware)],
    )
    client = TestClient(app, client=("127.0.0.1", 1234))
    r = client.get("/probe", headers={"X-MCP-Credential-Kind": "api_key"})

    assert r.status_code == 200
    assert r.text == "kind=api_key"
    # …and it is reset, so it can't bleed into the next request.
    assert mcp_credential_kind_var.get() is None


# ----- the shipped credential kind actually has a rehydrator --------------------


def test_api_key_kind_is_registered() -> None:
    """`api_keys.AppConfig.ready()` must register both halves. Registering
    the resolver alone is exactly the state this whole file exists to
    catch: auth works, tier silently doesn't."""
    assert "api_key" in bearer.rehydratable_kinds()
