"""Async streaming responses must not be gzip-compressed.

Django's GZipMiddleware has two streaming paths and they are not
equivalent. The sync path (`compress_sequence`) emits ONE gzip stream —
header once, Z_SYNC_FLUSH between chunks — which browsers decode
progressively. The async path compresses every chunk independently with
`compress_string`, so the response body is a CONCATENATION of complete
gzip members, one per chunk. Multi-member gzip is legal (RFC 1952 §2.2)
but Chromium's fetch decoder stops at the first member boundary, so an
SSE consumer sees exactly one frame and the stream then goes silent —
no error, the reader just reports done at connection close.

Proved live 2026-08-08 against the running stack: a raw capture of
`/ops/chat/<pk>/send` showed `\x1f\x8b` (a fresh gzip header) at the
head of EVERY network chunk, and the chat page rendered the first
delta — the single character "P" — then dropped every later frame,
including `done` (so sources and the token note never appeared) while
the full 668-token answer sat correctly in ChatMessage. Every byte the
server produced was right; the transport framing made Chromium discard
it.

The middleware class is resolved from settings.MIDDLEWARE, not imported
directly: the fix IS the swap to a stream-safe subclass, so these tests
fail against stock GZipMiddleware and fail again if anyone swaps it
back.
"""
from __future__ import annotations

from importlib import import_module

from django.conf import settings
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory


def _gzip_middleware():
    path = next(m for m in settings.MIDDLEWARE if "gzip" in m.lower())
    module, _, name = path.rpartition(".")
    # get_response is never reached by process_response; a stub is fine.
    return getattr(import_module(module), name)(lambda request: HttpResponse())


def _gzip_request():
    return RequestFactory().get("/probe/", HTTP_ACCEPT_ENCODING="gzip")


def test_async_streaming_response_is_not_compressed():
    """The broken path: async streaming → per-chunk gzip members."""

    async def frames():
        yield b'event: delta\ndata: {"text": "P"}\n\n'
        yield b'event: done\ndata: {}\n\n'

    response = StreamingHttpResponse(frames(), content_type="text/event-stream")
    out = _gzip_middleware().process_response(_gzip_request(), response)
    assert out.headers.get("Content-Encoding") != "gzip", (
        "async streaming response was gzip-compressed; Django's async "
        "path emits one gzip member per chunk and Chromium truncates "
        "multi-member gzip at the first boundary"
    )


def test_sync_streaming_response_still_compresses():
    """FileResponse-shaped streams keep compression: the sync path emits
    a single progressive gzip stream, which browsers handle fine."""
    response = StreamingHttpResponse(
        iter([b"x" * 500]), content_type="text/markdown"
    )
    out = _gzip_middleware().process_response(_gzip_request(), response)
    assert out.headers.get("Content-Encoding") == "gzip"


def test_buffered_response_still_compresses():
    response = HttpResponse(b"x" * 500, content_type="application/json")
    out = _gzip_middleware().process_response(_gzip_request(), response)
    assert out.headers.get("Content-Encoding") == "gzip"
