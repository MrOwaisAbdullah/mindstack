"""Scheduled-task callables for `apps.core`.

Declared in `config/scheduled.py`. Top-level callable; Q2 imports
dotted-path from a fresh worker process.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run_purge_idempotency_keys() -> dict:
    """Drop idempotency-key rows past their 24h TTL.

    Wraps `apps.core.idempotency.purge_expired` so the schedule entry's
    dotted-path is stable — same call path the
    `manage.py purge_idempotency_keys` command takes.
    """
    from apps.core.idempotency import DEFAULT_TTL_SECONDS, purge_expired

    deleted = purge_expired(ttl_seconds=DEFAULT_TTL_SECONDS)
    log.info(
        "purge_idempotency_keys scheduled: ttl_seconds=%s deleted=%s",
        DEFAULT_TTL_SECONDS,
        deleted,
    )
    return {"ttl_seconds": DEFAULT_TTL_SECONDS, "deleted": deleted}


__all__ = ["run_purge_idempotency_keys"]
