"""Typed domain event bus.

Apps don't call each other for side effects — they raise events. Other apps
subscribe. This file is the bus implementation plus the first cross-cutting
event (`EndpointCalled`); per-app events live in each app's `events.py`.

Why not Django signals directly:
- Pydantic payloads (typed, validated, serializable for replay/tests).
- A central registry the test harness can introspect.
- `freeze()` / `replay()` helpers for assertion-style tests.
- One mental model regardless of whether the subscriber is sync or async.

Subscribers are sync today. If a subscriber needs to do I/O on the request
path, wrap it in `sync_to_async` at the call site (see
`apps.observability.events`) — or hand off to a Q2 background task.
"""
from __future__ import annotations

import inspect
import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, ClassVar, Iterator

from pydantic import BaseModel

log = logging.getLogger(__name__)

# event_name → event_cls. Populated when an Event subclass is defined.
event_registry: dict[str, type["Event"]] = {}
# event_cls → list of handler callables.
_subscribers: dict[type["Event"], list[Callable[[Any], None]]] = {}
# Per-thread "freeze" state. None = normal dispatch. Else, list collects events.
_local = threading.local()


class Event(BaseModel):
    """Base class for all domain events.

    Subclasses set `event_name: ClassVar[str] = "<app>.<entity>.<action>"`
    (matches the canonical-events list in CLAUDE.md). The bus uses this name
    as the registry key and as the value the audit-log subscriber records.

    Pydantic v2 model: `model_config = {"frozen": True}` is the default
    intent — events are immutable once fired. Subclasses that need extra
    config can override.
    """

    event_name: ClassVar[str]

    model_config = {"frozen": True}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "event_name", None)
        if not name:
            raise RuntimeError(
                f"{cls.__module__}.{cls.__name__} must declare "
                "`event_name: ClassVar[str] = ...`"
            )
        existing = event_registry.get(name)
        if existing is not None and existing is not cls:
            raise RuntimeError(
                f"Duplicate event_name {name!r}: "
                f"{existing.__module__}.{existing.__name__} vs "
                f"{cls.__module__}.{cls.__name__}"
            )
        event_registry[name] = cls


def subscribe(event_cls: type[Event]) -> Callable[[Callable[[Any], None]], Callable[[Any], None]]:
    """Register `handler` to fire whenever `event_cls` (or a subclass) is fired.

    Use as a decorator from each app's `events.py`:

        from apps.core.events import EndpointCalled, subscribe

        @subscribe(EndpointCalled)
        def write_api_call_log(event: EndpointCalled) -> None:
            ...
    """

    def _wrap(handler: Callable[[Any], None]) -> Callable[[Any], None]:
        _subscribers.setdefault(event_cls, []).append(handler)
        return handler

    return _wrap


def fire(event: Event) -> None:
    """Dispatch an event to every subscriber registered on its class or any base.

    Inside `freeze()` the event is recorded but no subscriber runs — useful
    for tests that want to assert on emitted events without triggering side
    effects. `replay(events)` re-fires captured events afterwards if needed.

    Subscriber exceptions are caught + logged — one bad subscriber must not
    break the request path. Bus integrity > subscriber correctness.
    """
    if _frozen_buffer() is not None:
        _frozen_buffer().append(event)
        return

    for cls in type(event).__mro__:
        if cls is Event or cls is BaseModel or cls is object:
            break
        for handler in _subscribers.get(cls, ()):
            try:
                if inspect.iscoroutinefunction(handler):
                    log.warning(
                        "events.fire(%s): handler %s is async; sync-bus dropped it. "
                        "Use a sync wrapper that schedules the async work.",
                        event.event_name,
                        handler,
                    )
                    continue
                handler(event)
            except Exception:
                log.exception(
                    "events.fire(%s): subscriber %s raised",
                    event.event_name,
                    handler,
                )


@contextmanager
def freeze() -> Iterator[list[Event]]:
    """Suppress subscribers and collect every fired event into a list.

        with freeze() as fired:
            await call_endpoint(...)
        assert any(e.event_name == "endpoints.endpoint.called" for e in fired)

    Nested freezes share the outermost buffer (additive — second `with`
    reuses the first's list).
    """
    existing = _frozen_buffer()
    if existing is not None:
        yield existing
        return
    buf: list[Event] = []
    _local.frozen = buf
    try:
        yield buf
    finally:
        _local.frozen = None


def replay(events: list[Event]) -> None:
    """Re-fire a list of events through the live bus (subscribers will run)."""
    for event in events:
        fire(event)


def _frozen_buffer() -> list[Event] | None:
    return getattr(_local, "frozen", None)


# ---- First cross-cutting event ----------------------------------------------
# Defined here (not in apps.observability.events) because every endpoint call
# fires it regardless of whether observability is even installed; it's part of
# the @endpoint contract surface, like APICallLog itself but more abstract.


class EndpointCalled(Event):
    """One @endpoint was invoked over REST or MCP. Phase 2.6.3."""

    event_name: ClassVar[str] = "endpoints.endpoint.called"

    request_id: str
    endpoint_slug: str
    source: str  # "rest" | "mcp"
    status_code: int
    latency_ms: int
    user_id: int | None = None
    credential_id: int | None = None
    # Which table `credential_id` is a pk in — "api_key", "url_token", …
    # A bare pk is ambiguous now that two credential types authenticate
    # MCP callers: APIKey 3 and URLMCPToken 3 are different rows, and a
    # subscriber that assumes its own table would stamp the wrong one.
    # Empty means "not stated"; subscribers predating this field read
    # that as their own kind.
    credential_kind: str = ""
    error_class: str = ""
    ip: str | None = None
    user_agent: str = ""
