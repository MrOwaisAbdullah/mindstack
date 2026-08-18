"""Per-call execution context.

Every `Endpoint.run(inp, ctx)` receives a `Ctx` instance built fresh per
request. It carries everything the endpoint might need that isn't part of
the typed input: identity, request id, transport source, current time,
and a `.trace` proxy for recording handled-but-noteworthy errors to the
event log.

It used to also carry a background-job surface — `enqueue` / `aenqueue`
/ `report_progress` / `defer_to_webhook`, returning a `JobHandle` the
caller polled at `/api/v1/_jobs/<id>`. All of it dispatched through
`apps.core.jobs_hook`, which nothing registered against, and no endpoint
in this product ever called it: real background work here goes straight
to `django_q.tasks.async_task`. Removed before launch along with the
hook.

Identity (`user`, `credential`) is resolved from the authenticated
principal — magic-link sessions, API keys, or OAuth tokens — before
`run()` is called.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

Source = Literal["rest", "mcp", "test", "playground"]


class _TraceProxy:
    """Namespace exposed via `ctx.trace.*`.

    Lives on the parent `Ctx` so the call site stays readable:

        ctx.trace.exception(exc, message="couldn't reach foo, falling back")

    Records a handled error event (`handled=True`, `status_code=200`).
    The endpoint kept running and returned a real response — this is for
    operator visibility into degraded paths, not for marking the request
    as failed.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: "Ctx") -> None:
        self._ctx = ctx

    def exception(
        self,
        exc: BaseException,
        *,
        endpoint_slug: str = "",
        message: str | None = None,
    ) -> None:
        """Persist `exc` as a handled error event.

        `message` is appended to `exc.args[0]` so the recorded
        `exc_message` reflects what the endpoint chose to surface.
        """
        from apps.core import error_hook

        if message:
            try:
                exc.args = ((str(exc.args[0]) if exc.args else "") + " | " + message,) + exc.args[1:]
            except Exception:
                pass

        user_id = (
            self._ctx.user.pk
            if (self._ctx.user is not None and self._ctx.user.is_authenticated)
            else None
        )
        try:
            error_hook.record_error(
                exc=exc,
                request_id=self._ctx.request_id,
                source=self._ctx.source,
                endpoint_slug=endpoint_slug or self._ctx.meta.get("endpoint_slug", ""),
                status_code=200,
                user_id=user_id,
                handled=True,
            )
        except Exception:
            # Tracing must never break the request path.
            pass


@dataclass(slots=True)
class Ctx:
    """Per-call execution context handed to `Endpoint.run`."""

    request_id: str
    source: Source
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user: Optional["AbstractBaseUser"] = None
    credential: Any = None  # APIKey | OAuthToken | None — typed in Phase 3.3 / 4.3
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def trace(self) -> "_TraceProxy":
        """`ctx.trace.exception(exc, message="...")` writes a handled
        error event. For operator visibility into degraded paths the
        endpoint chose to recover from."""
        return _TraceProxy(self)


def build_ctx(
    *,
    request_id: str,
    source: Source,
    user: Optional["AbstractBaseUser"] = None,
    credential: Any = None,
) -> Ctx:
    """Factory used by `EndpointView` (REST) and the MCP bridge.

    Kept as a factory rather than calling `Ctx(...)` directly so later phases
    can inject defaults (locale, request-scoped DB hints) without churning
    every call site.
    """
    return Ctx(request_id=request_id, source=source, user=user, credential=credential)
