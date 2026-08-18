"""Scheduled-task callables for `apps.reader`.

Declared in `config/scheduled.py`. Top-level callables; Q2 imports them by
dotted path from a fresh worker process.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7


def run_cleanup_sdk_transcripts(days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Delete Claude CLI session transcripts past their retention window.

    The bundled CLI writes JSONL session files under ~/.claude/projects/ in
    every container that runs SDK agents, and they contain note content in
    plaintext. The management command that removes them has existed since
    the retention promise was made, but nothing ever called it — no cron,
    no Q2 schedule, no mention in DEPLOY.md — so transcripts accumulated
    forever in the web and worker containers.
    """
    cutoff = time.time() - days * 86400
    root = Path.home() / ".claude" / "projects"
    removed = 0
    if root.is_dir():
        for p in root.rglob("*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    log.info("cleanup_sdk_transcripts: days=%s removed=%s", days, removed)
    return {"days": days, "removed": removed}


__all__ = ["run_cleanup_sdk_transcripts", "DEFAULT_RETENTION_DAYS"]
