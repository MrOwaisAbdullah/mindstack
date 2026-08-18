"""URL-path token scrubbing.

The threat: when the credential lives in `/mcp/k/<mcpurl_FullSecret>/`,
every layer that touches `request.path` is a leak surface — Django
access logs, our structured JSON logger, APICallLog `request_path`,
ErrorLog rows, Sentry breadcrumbs, nginx/Caddy access logs sitting in
front of uvicorn.

Three coordinated mitigations land in this module:

1. **`URLTokenScrubMiddleware`** — runs BEFORE `RequestIdMiddleware` so
   its attributes are published before any inner middleware runs. It
   stashes the plaintext token at `request._url_token_plain` (only the
   proxy view reads it) and publishes a scrubbed copy of the path at
   `request._scrubbed_path`.

   It does NOT rewrite `request.path`, `request.path_info` or
   `META["PATH_INFO"]` — see the class docstring for why that would 404
   the very route this protects. **`request.path` stays dirty for the
   whole request**, so any new code that persists a path must read
   `_scrubbed_path`, and anything that logs `request.path` relies on
   `ScrubLogFilter` (3) to catch it downstream.

2. **`scrub_url_token(s)`** — pure regex helper, exported so the JSON
   log formatter's pre-filter and Sentry's `before_send` hook can
   defense-in-depth scrub any string that might still slip through
   (a logger that captured `request.path` BEFORE the middleware ran,
   or any other unsanitized source). Same rewrite rule as the
   middleware.

3. **`ScrubLogFilter`** — `logging.Filter` subclass that runs the
   scrub regex on every record's `msg` + `args`. Wired into the
   stdout handler in `config/logging.py` so prod JSON logs are
   guaranteed clean even if a future call site forgets to scrub.

The scrub format is `mcpurl_<8-char-prefix>***`. We keep the 8-char
prefix so operator incident response can still correlate "which token
got abused" without leaking the secret half — same trick the dashboard
uses to display tokens in the UI.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse

# Group 1 captures the 8-char prefix slice we keep visible. The full
# token is `mcpurl_` (7) + 32-byte url-safe body (~43 chars), so the
# pattern matches at least 9 body chars (defensive: still scrubs a
# malformed or truncated token rather than letting it through). The
# trailing class accepts every url-safe-base64 char + `=` padding.
_TOKEN_RE = re.compile(r"mcpurl_([A-Za-z0-9_\-]{8})[A-Za-z0-9_\-=]+")


def scrub_url_token(s: str) -> str:
    """Rewrite every `mcpurl_<full>` substring in `s` to `mcpurl_<8>***`.

    Pure function; safe to call from any log / Sentry hook. Handles
    multiple occurrences (e.g. an exception traceback that quotes a
    URL twice) in one pass.
    """
    if not s or "mcpurl_" not in s:
        return s
    return _TOKEN_RE.sub(r"mcpurl_\1***", s)


# ----- Middleware -----------------------------------------------------------


class URLTokenScrubMiddleware:
    """Publish a log-safe copy of the path, and the plaintext token, on
    every request that carries a `mcpurl_*` credential in its URL.

    It does NOT rewrite `request.path`, and that is the whole design.
    Middleware runs BEFORE URL dispatch, so a rewritten path is the path
    the dispatcher then tries to resolve — which 404s the very route this
    exists to protect. Instead it publishes two attributes alongside the
    original:

    - `request._scrubbed_path` — the `mcpurl_<8>***` form. Every writer
      that persists a path (`ErrorLog` via `apps.core.rest`) reads this
      instead of `request.path`.
    - `request._url_token_plain` — the raw token, read by exactly one
      caller, the proxy view's url-token branch.

    The dispatcher keeps seeing the real path and the view keeps
    receiving the plaintext through its `token` kwarg, so nothing about
    routing changes. Log records that captured `request.path` directly
    are caught downstream by `ScrubLogFilter`, which is the reason that
    filter exists rather than being redundant with this.

    Ordering: install BEFORE `RequestIdMiddleware`, so the attributes are
    already published when the first inner middleware runs.

    Sync AND async capable, which placement demands. `RequestIdMiddleware`
    is async-only; a sync-only middleware installed outside it makes
    Django adapt the entire inner chain with `async_to_sync`, which runs
    every request through a thread and spins up a second event loop for
    the proxy's streaming responses. The dual-mode shape below is
    Django's documented recipe for avoiding that.
    """

    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponse],
    ) -> None:
        self.get_response = get_response
        self._async_mode = iscoroutinefunction(get_response)
        if self._async_mode:
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest):
        if self._async_mode:
            return self.__acall__(request)
        self._publish(request)
        return self.get_response(request)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        self._publish(request)
        return await self.get_response(request)  # type: ignore[misc]

    @staticmethod
    def _publish(request: HttpRequest) -> None:
        path = request.path or ""
        if "mcpurl_" in path:
            # The plaintext token (everything between `/mcp/k/` and the
            # next `/` or end of path) so the proxy view can resolve it
            # without reading the scrubbed path.
            m = re.search(
                r"/mcp/k/(mcpurl_[A-Za-z0-9_\-=]+)", path
            )
            if m is not None:
                request._url_token_plain = m.group(1)  # type: ignore[attr-defined]
            request._scrubbed_path = scrub_url_token(path)  # type: ignore[attr-defined]
        else:
            request._scrubbed_path = path  # type: ignore[attr-defined]


# ----- Logging filter (defense in depth) ------------------------------------


class ScrubLogFilter(logging.Filter):
    """Scrub `mcpurl_*` tokens out of every log record before it's
    formatted.

    Catches the case where a log call captured `request.path` (or any
    other dirty string) BEFORE the middleware rewrite landed, or where
    an external library logs a URL we never sanitized. Cheap: one
    regex `search` per record on the message + each arg + a small set
    of well-known leak-prone extras.

    Beyond `msg` + `args`, we also scrub:

    - `record.request` — Django's `django.request` logger attaches the
      raw `HttpRequest` here as an extra. The JSON formatter then
      serializes `repr(request)`, which embeds `request.path` verbatim.
      Without this scrub, every 4xx/5xx Django response logs the full
      URL-path token even though our `message` field is clean.
    - `record.exc_text` — cached formatted traceback. If an exception
      mentions a URL that contains the token (e.g. an `httpx` retry
      log), the traceback gets persisted unscrubbed.
    - `record.stack_info` — similar to `exc_text` but for `stack_info=True`
      logger calls.
    """

    _EXTRAS_TO_SCRUB = ("exc_text", "stack_info")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = scrub_url_token(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: scrub_url_token(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        scrub_url_token(a) if isinstance(a, str) else a
                        for a in record.args
                    )
            # Django's `django.request` logger attaches the HttpRequest
            # as `extra={"request": request}`. Coerce to a scrubbed
            # repr string so the JSON formatter (and any other consumer)
            # sees a safe value.
            req = getattr(record, "request", None)
            if req is not None:
                record.request = scrub_url_token(
                    req if isinstance(req, str) else repr(req)
                )
            for attr in self._EXTRAS_TO_SCRUB:
                val = getattr(record, attr, None)
                if isinstance(val, str) and "mcpurl_" in val:
                    setattr(record, attr, scrub_url_token(val))
        except Exception:
            # Logging filters MUST NOT raise — they run on every record.
            # Worst case here is a malformed args object; let the
            # record through unscrubbed rather than dropping it.
            pass
        return True


# ----- Sentry before_send hook ----------------------------------------------


def sentry_scrub_url_token(event: dict, _hint: dict) -> dict:
    """Sentry `before_send` hook — rewrites `mcpurl_<full>` in the
    request URL + query string + every breadcrumb data.url.

    Returns the event mutated in place (Sentry's documented contract).
    Returns the event even if scrubbing fails — never drop an error
    report because of a sanitization bug.
    """
    try:
        req = event.get("request") or {}
        if isinstance(req.get("url"), str):
            req["url"] = scrub_url_token(req["url"])
        if isinstance(req.get("query_string"), str):
            req["query_string"] = scrub_url_token(req["query_string"])
        # Breadcrumbs is a list of dicts; their `.data.url` is where
        # the SDK records each outgoing HTTP call. Scrub everything
        # string-shaped that lives at `breadcrumb["data"]["url"]`.
        for crumb in event.get("breadcrumbs", {}).get("values", []) or []:
            data = crumb.get("data") or {}
            if isinstance(data.get("url"), str):
                data["url"] = scrub_url_token(data["url"])
    except Exception:
        pass
    return event


__all__ = [
    "ScrubLogFilter",
    "URLTokenScrubMiddleware",
    "scrub_url_token",
    "sentry_scrub_url_token",
]
