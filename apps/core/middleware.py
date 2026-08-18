"""Request-ID propagation middleware.

Reads `X-Request-ID` from inbound or mints a uuid4 hex string. The same
value rides every later hop:

- Stored on `request.request_id` (read by `EndpointView` and the proxy view).
- Bound into a `ContextVar` (`apps.core.log_context.request_id_var`) so any
  log record emitted during this request — anywhere in the call chain —
  picks it up via the `RequestContextFilter`.
- Echoed on the response as `X-Request-ID` (always — for client-side
  support tickets).
- Forwarded to the MCP subprocess as `X-MCP-Request-Id`.
- Recorded on every `APICallLog` row.

A single id traces a request from nginx → uvicorn → Django view → MCP
subprocess → APICallLog. The dedicated middleware lives here (in core)
because it's a cross-cutting concern, not specific to observability.
"""
from __future__ import annotations

import uuid
from typing import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.http import HttpRequest, HttpResponse
from django.middleware.gzip import GZipMiddleware
from whitenoise.middleware import WhiteNoiseMiddleware

from apps.core import log_context


class RequestIdMiddleware:
    """Outermost middleware. Always sets request.request_id + response header."""

    sync_capable = False
    async_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], Awaitable[HttpResponse]]) -> None:
        self.get_response = get_response
        markcoroutinefunction(self)
        if not iscoroutinefunction(get_response):
            raise RuntimeError(
                "RequestIdMiddleware requires the async ASGI stack. "
                "Uvicorn wires this automatically."
            )

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.request_id = request_id  # type: ignore[attr-defined]
        # Bind the contextvar so log records emitted during this request
        # auto-carry request_id. user_id is left None here — bearer
        # resolution happens later in the chain (apps.core.rest +
        # apps.mcp_proxy.views) and those layers re-bind once the
        # Principal is known.
        token = log_context.bind(request_id=request_id, user_id=None)
        try:
            response = await self.get_response(request)
        finally:
            log_context.unbind(token)
        # Set unconditionally — overwriting any inner-middleware echo so the
        # client always sees the canonical id we recorded.
        response.headers["X-Request-ID"] = request_id
        return response


class StreamSafeGZipMiddleware(GZipMiddleware):
    """GZipMiddleware that leaves async streaming responses alone.

    Upstream's two streaming paths are not equivalent. The sync path
    (`compress_sequence`) emits ONE gzip stream — header once,
    Z_SYNC_FLUSH between chunks — and browsers decode it progressively.
    The async path runs `compress_string` on EVERY chunk, so the body
    is a concatenation of complete gzip members, one per chunk.
    Multi-member gzip is legal (RFC 1952 §2.2), but Chromium's fetch
    decoder stops at the first member boundary: an SSE consumer renders
    the first frame and the stream then goes silent with no error.

    On this app that was the whole chat surface — the bubble showed the
    first delta (one character) while the full answer sat correctly in
    ChatMessage, and the `done` frame (sources, token note) never
    reached the page. The MCP proxy's SSE listen channel is the same
    shape. Async streaming responses here are SSE, where compression
    buys nothing worth the framing risk; buffered and sync-streaming
    responses keep upstream behaviour untouched.
    """

    def process_response(self, request, response):
        if response.streaming and getattr(response, "is_async", False):
            return response
        return super().process_response(request, response)


class AsyncWhiteNoiseMiddleware(WhiteNoiseMiddleware):
    """whitenoise's middleware, made async-capable.

    Upstream `WhiteNoiseMiddleware` declares no `async_capable`, so in
    this stack — where every other middleware is async-only — Django
    adapted it and the ENTIRE chain inside it back to sync: four
    sync/async boundary crossings and two pinned threadpool threads on
    every request. `assemble-context` at 5–30 s plus the 4 s
    `activity.json` poll saturates that pool silently — exactly the
    pathology `log_scrub` documents and designs around at the outer
    edge, reintroduced mid-chain by a static-file helper.

    The hit-path work is safe inline: with `autorefresh` off (any real
    deployment) the lookup is a dict get over an in-memory manifest,
    and `serve` opens one local file for Django's FileResponse — the
    same synchronous `open()` every FileResponse does; the ASGI handler
    streams it without holding a thread. `autorefresh` (a DEBUG mode)
    re-scans the filesystem per request, so that path keeps a thread
    hop.
    """

    sync_capable = False
    async_capable = True

    def __init__(self, get_response) -> None:
        super().__init__(get_response)
        markcoroutinefunction(self)
        if not iscoroutinefunction(get_response):
            raise RuntimeError(
                "AsyncWhiteNoiseMiddleware requires the async ASGI stack."
            )

    async def __call__(self, request: HttpRequest) -> HttpResponse:  # type: ignore[override]
        # Mirrors upstream __call__ line for line; only the waiting is new.
        if self.autorefresh:
            static_file = await sync_to_async(self.find_file, thread_sensitive=False)(
                request.path_info
            )
        else:
            static_file = self.files.get(request.path_info)
        if static_file is not None:
            return self.serve(static_file, request)
        return await self.get_response(request)
