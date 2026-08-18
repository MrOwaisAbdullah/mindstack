"""Request-scoped logging context.

A pair of `ContextVar`s carrying `request_id` + `user_id` for the duration
of a single request. The `RequestIdMiddleware` (apps.core.middleware) sets
these on entry and resets on exit; the `RequestContextFilter` in
`apps.observability.log_filter` reads them so every log record emitted
inside the request handler — anywhere in the call chain, including
subscribers, services, sync_to_async threads — automatically carries
`request_id` + `user_id`.

Why a contextvar (not threadlocal): we run on uvicorn (ASGI), so a request
may bounce between the event loop and worker threads via `sync_to_async`.
`ContextVar.set()` correctly copies into the worker thread context for
each `sync_to_async` call. Threadlocals would not.

Why this lives in `apps.core` (not `apps.observability`): it's a
cross-cutting concern set by middleware that lives in apps.core, and
import-linter Contract 1 forbids `apps.core` from importing
`apps.observability`. The filter is the only piece in observability —
both modules pull state from this single source.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import NamedTuple

# `default=None` so log records emitted outside any request (Q2 worker
# starting up, management commands, MCP subprocess boot) don't blow up.
# The filter renders an empty `request_id=` segment in that case.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[int | None] = ContextVar("user_id", default=None)


class _BindToken(NamedTuple):
    """Pair of `ContextVar.set()` tokens — pass back to `unbind()` to reset."""

    request_id: Token[str | None]
    user_id: Token[int | None]


def bind(*, request_id: str | None, user_id: int | None = None) -> _BindToken:
    """Set both contextvars for the duration of a request.

    Returns a token-pair the caller passes to `unbind()`. We use Token-based
    reset rather than `var.set(None)` because `Token.var.reset(token)` is the
    only way to restore the previous value when contexts are nested (e.g.
    a middleware chain inside a Q2 worker that already has its own context).
    """
    return _BindToken(
        request_id=request_id_var.set(request_id),
        user_id=user_id_var.set(user_id),
    )


def unbind(token: _BindToken) -> None:
    """Restore both contextvars to their pre-`bind()` values."""
    request_id_var.reset(token.request_id)
    user_id_var.reset(token.user_id)


def update_user_id(user_id: int | None) -> None:
    """Set the bound `user_id` mid-request (after bearer resolution).

    `RequestIdMiddleware` calls `bind()` with `user_id=None` because auth
    runs further down the chain. Once the view resolves a Principal it
    calls this to backfill. The token returned by `set()` is discarded
    because the outer `unbind(token_from_bind)` already resets the var
    to its pre-request value (which is what we want).
    """
    user_id_var.set(user_id)


def get_request_id() -> str | None:
    return request_id_var.get()


def get_user_id() -> int | None:
    return user_id_var.get()
