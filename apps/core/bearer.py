"""Bearer-token resolver registry.

`apps.core.rest.EndpointView` only knows about *bearer authentication in
the abstract*. Concrete backends (API key, OAuth 2.1 access token) live
in their own apps and register themselves here at startup. This keeps
the `apps.core` layer pure — it has no reverse dependency on a feature
app, which import-linter Contract 1 enforces.

Each registered resolver is an async callable `(token: str) -> Principal | None`.
We try them in registration order and return the first match, so the
order at boot determines priority. For Phase 3 only the API-key resolver
exists; Phase 4.3 adds OAuth and registers it after API keys (cheaper
prefix match wins).

Resolvers are sync DB-bound today; the wrapper here adapts each one
through `sync_to_async` so the request loop stays non-blocking. Phase 8.2
will introduce a Redis cache layer in front of the resolvers themselves.

Rehydrators
-----------
The MCP subprocess never sees the bearer token — the proxy strips it and
forwards the *resolved* identity as `(user_id, credential_kind,
credential_id)` on `X-MCP-*` headers. To rebuild a `Ctx` on the far side
of that hop the subprocess has to turn `(kind, pk)` back into the
credential row, and it has the same Contract-1 problem the resolvers
have: `apps.core` cannot import `apps.api_keys`. So the owning app
registers a rehydrator here too, next to its resolver.

A rehydrator must re-apply the same liveness guards its resolver
applies (revoked / expired / deleted / inactive user). The subprocess
trusts its caller, so the guards are what keep a stale or forged id from
resolving to a live credential.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from asgiref.sync import sync_to_async

if TYPE_CHECKING:
    from apps.core.principal import Principal

# (name, sync_callable) tuples. The name is logged on resolution + makes
# debugging "which backend authenticated this caller" tractable.
_resolvers: list[tuple[str, Callable[[str], "Principal | None"]]] = []

# credential_kind -> sync callable (pk) -> credential row | None.
_rehydrators: dict[str, Callable[[str], Any]] = {}


def register(name: str, resolver: Callable[[str], "Principal | None"]) -> None:
    """Add a resolver. Call from `AppConfig.ready()` of the owning app."""
    _resolvers.append((name, resolver))


def register_rehydrator(kind: str, fn: Callable[[str], Any]) -> None:
    """Teach the resolver how to turn a `(credential_kind, pk)` pair back
    into a credential row. `kind` must match the `Principal.credential_kind`
    the matching resolver returns. Call from `AppConfig.ready()`."""
    _rehydrators[kind] = fn


async def resolve(token: str) -> "Principal | None":
    """Try every registered resolver. First match wins. None = unauthenticated."""
    for _name, fn in _resolvers:
        principal = await sync_to_async(fn, thread_sensitive=True)(token)
        if principal is not None:
            return principal
    return None


async def rehydrate(kind: str, credential_id: str) -> Any:
    """Rebuild a credential row from `(kind, pk)`, or None.

    None for an unregistered kind — the caller then runs with no
    credential, which every tier check reads as least-privilege. That is
    the safe direction: a kind we don't understand must not resolve to
    a credential we'd grant a tier to.
    """
    fn = _rehydrators.get(kind)
    if fn is None:
        return None
    return await sync_to_async(fn, thread_sensitive=True)(credential_id)


def registered_names() -> list[str]:
    """Introspection helper — used by `manage.py check` extensions if any."""
    return [name for name, _ in _resolvers]


def rehydratable_kinds() -> list[str]:
    """Introspection helper — mirrors `registered_names()`."""
    return sorted(_rehydrators)
