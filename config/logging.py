"""Project-wide logging configuration.

Two formatters, picked by environment:

- **console** (dev) — colour-friendly, single line, request_id appended:
      `2026-05-07 12:34:56 INFO  apps.core.rest [request_id=ab12cd]: hello`
- **json** (prod) — one JSON object per record. Log shippers (Loki,
  Datadog, CloudWatch) parse without a regex.

`build_logging(debug=...)` is called from `config/settings/base.py`. dev
settings call with `debug=True`; prod with `debug=False`. Either way the
`RequestContextFilter` decorates every record with
`request_id` + `user_id` pulled from the contextvar that
`RequestIdMiddleware` binds per request.

The Django logger is set to WARNING by default (we already log every
request via `RequestLogMiddleware`'s `EndpointCalled` event subscriber +
have full APICallLog rows — Django's repeated "Bad Request: ..." noise
isn't useful on top of that). `apps.*` loggers run at INFO so service
emits show up; bump to DEBUG via env var if needed.
"""
from __future__ import annotations

from typing import Any


def build_logging(*, debug: bool) -> dict[str, Any]:
    """Return the Django LOGGING dict.

    Picks console formatter when `debug=True`, JSON formatter otherwise.
    `RequestContextFilter` runs on every handler so log records inside a
    request pick up the bound `request_id` / `user_id`.
    """
    formatter_name = "console" if debug else "json"

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_context": {
                "()": "apps.core.log_filter.RequestContextFilter",
            },
            # defense-in-depth scrub of `mcpurl_*` URL-path
            # tokens out of every log record. The middleware path-scrub
            # is the primary mitigation (callers never write a dirty
            # path); this filter catches any stray `request.path`
            # capture or third-party library log line that slipped
            # through. Sub-millisecond per record (one regex search).
            "scrub_url_token": {
                "()": "apps.core.security.log_scrub.ScrubLogFilter",
            },
        },
        "formatters": {
            "console": {
                "format": (
                    "%(asctime)s %(levelname)-5s %(name)s "
                    "[request_id=%(request_id)s]: %(message)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "json": {
                # python-json-logger v3+ entry point — `pythonjsonlogger.json.JsonFormatter`
                # emits one JSON object per record. The format string lists the
                # fields it pulls off the LogRecord; extra `extra={...}` kwargs
                # passed to log calls are also serialized.
                "()": "pythonjsonlogger.json.JsonFormatter",
                "format": (
                    "%(asctime)s %(levelname)s %(name)s %(message)s "
                    "%(request_id)s %(user_id)s"
                ),
                "rename_fields": {
                    "asctime": "ts",
                    "levelname": "level",
                    "name": "logger",
                },
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                # Order matters: scrub THEN bind request_context. The
                # context filter only adds fields; the scrub filter
                # rewrites the message before any formatter sees it.
                "filters": ["scrub_url_token", "request_context"],
                "formatter": formatter_name,
            },
        },
        "root": {
            "handlers": ["stdout"],
            "level": "INFO",
        },
        "loggers": {
            # Django's request logger drops to WARNING — we have APICallLog
            # rows + EndpointCalled events covering the same surface.
            "django": {"level": "WARNING", "propagate": True},
            "django.server": {"level": "WARNING", "propagate": True},
            # uvicorn's access + lifespan loggers; mirror gunicorn's level
            # discipline so dev tail isn't a wall of HTTP/handshake noise.
            "uvicorn": {"level": "WARNING", "propagate": True},
            "uvicorn.access": {"level": "WARNING", "propagate": True},
            # Our apps stay at INFO so service-layer `log.info(...)` emits land.
            "apps": {"level": "INFO", "propagate": True},
            "config": {"level": "INFO", "propagate": True},
            # Gunicorn writes its access log line ("IP - METHOD PATH STATUS")
            # via Python's logging system on the `gunicorn.access` logger.
            # By default gunicorn installs its own bare handler at boot,
            # which bypasses our `scrub_url_token` filter — so URL-path
            # tokens land in plaintext access logs. Pin both gunicorn
            # loggers to our `stdout` handler (which carries the scrub
            # filter) and `propagate=False` so they don't double-emit
            # through root.
            "gunicorn.access": {
                "handlers": ["stdout"],
                "level": "INFO",
                "propagate": False,
            },
            "gunicorn.error": {
                "handlers": ["stdout"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
