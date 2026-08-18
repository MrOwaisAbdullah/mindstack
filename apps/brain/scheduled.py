"""Scheduled-task callables for `apps.brain`.

Declared in `config/scheduled.py`. Top-level callables; Q2 imports them by
dotted path from a fresh worker process.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run_sync_brain() -> dict:
    """Periodic pull + reindex + snapshot rebuild.

    The webhook is the fast path, but it is optional and it can miss: a
    delivery fails, the secret is unset, the repo is pushed from somewhere
    that isn't GitHub. Without this beat the server served a stale brain
    indefinitely and nothing said so — several failure paths in the sync
    and approval code logged "the sync beat will repair it" while no beat
    existed.
    """
    from apps.brain.services import sync

    run = sync.sync(trigger="beat")
    log.info(
        "sync_brain beat: ok=%s sha=%s added=%s changed=%s removed=%s",
        run.ok,
        (run.commit_sha or "")[:12],
        run.added,
        run.changed,
        run.removed,
    )
    return {
        "ok": run.ok,
        "commit_sha": run.commit_sha,
        "added": run.added,
        "changed": run.changed,
        "removed": run.removed,
    }


__all__ = ["run_sync_brain"]
