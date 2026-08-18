"""Error-recording hook registry.

`Ctx.trace.exception(...)` and `apps.core.rest.EndpointView` need to write
an `ErrorLog` row when something goes wrong — but apps.core cannot import
apps.observability (import-linter Contract 1: core has no reverse deps on
feature apps). This module is the bridge: apps.observability registers a
recorder at boot, apps.core dispatches without knowing the implementation.

Same shape as `apps.core.bearer`. When no recorder is registered,
`record_error()` is a silent no-op so request handling is never broken
by an unwired recorder.

In this fork the recorder is `apps.events.sinks.record_error`, installed
by `EventsConfig.ready()`; rows land in the `Event` log.

The recorder contract:

    recorder(
        *,
        exc: BaseException,
        request_id: str,
        source: str,           # "rest" | "mcp" | "test" | ...
        endpoint_slug: str,
        request_path: str,
        request_method: str,
        status_code: int,
        user_id: int | None,
        ip: str | None,
        user_agent: str,
        handled: bool,         # True for `Ctx.trace.exception`, False for uncaught
    ) -> str | None             # ErrorLog.id when persisted, None on best-effort failure
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol


class ErrorRecorder(Protocol):
    def __call__(
        self,
        *,
        exc: BaseException,
        request_id: str = "",
        source: str = "",
        endpoint_slug: str = "",
        request_path: str = "",
        request_method: str = "",
        status_code: int = 500,
        user_id: int | None = None,
        ip: str | None = None,
        user_agent: str = "",
        handled: bool = False,
    ) -> str | None: ...


_recorder: Optional[ErrorRecorder] = None


def register(recorder: ErrorRecorder) -> None:
    """Install the error recorder. Called from `ObservabilityConfig.ready()`."""
    global _recorder
    _recorder = recorder


def is_enabled() -> bool:
    return _recorder is not None


def record_error(
    *,
    exc: BaseException,
    request_id: str = "",
    source: str = "",
    endpoint_slug: str = "",
    request_path: str = "",
    request_method: str = "",
    status_code: int = 500,
    user_id: int | None = None,
    ip: str | None = None,
    user_agent: str = "",
    handled: bool = False,
) -> str | None:
    """Write an `ErrorLog` row via the registered recorder. No-op when none."""
    if _recorder is None:
        return None
    return _recorder(
        exc=exc,
        request_id=request_id,
        source=source,
        endpoint_slug=endpoint_slug,
        request_path=request_path,
        request_method=request_method,
        status_code=status_code,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        handled=handled,
    )


# Optional callable hook to test that the bridge is reachable without
# importing observability. `unregister()` is exposed for tests that want
# to assert the no-op fallback behavior.
def unregister() -> Callable[[], None]:
    global _recorder
    previous = _recorder
    _recorder = None

    def restore() -> None:
        global _recorder
        _recorder = previous

    return restore
