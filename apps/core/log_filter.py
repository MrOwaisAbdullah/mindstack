"""Logging filter that decorates every record with request context.

Pulled in by `config/logging.py`'s LOGGING dict. The filter reads
`apps.core.log_context.request_id_var` + `user_id_var` and copies their
values onto the `LogRecord`, so:

- The console formatter (dev) renders `[request_id=abc123] message`.
- The JSON formatter (prod) emits `{"request_id": "abc123", ...}`.
- Sentry events get tagged with the same id.

Records emitted outside any request (Q2 worker boot, management commands,
MCP subprocess startup) get blank `request_id` / `user_id` fields rather
than failing the format string.

Filters never reject records; they always return True. We only use the
filter mechanism because it's the standard `logging` extension point that
runs before the formatter.
"""
from __future__ import annotations

import logging

from apps.core import log_context


class RequestContextFilter(logging.Filter):
    """Decorate every record with `request_id` + `user_id` from the contextvar."""

    def filter(self, record: logging.LogRecord) -> bool:
        # `getattr` so manually-attached values via `extra={"request_id": "..."}`
        # win over the contextvar — useful in tests and for cross-request
        # correlation in Q2 hooks where the worker re-injects the original id.
        existing_request_id = getattr(record, "request_id", None)
        if not existing_request_id:
            record.request_id = log_context.get_request_id() or "-"
        existing_user_id = getattr(record, "user_id", None)
        if existing_user_id is None:
            record.user_id = log_context.get_user_id()
        return True
