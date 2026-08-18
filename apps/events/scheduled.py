"""Scheduled-task callables for `apps.events`.

Declared in `config/scheduled.py`. Top-level callables; Q2 imports them by
dotted path from a fresh worker process.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run_prune_event_log() -> dict:
    """Drop Event and finished SdkOperation rows past the retention window.

    Neither table had any pruning path at all — no cron, no ops action, no
    cascade that would ever reach them — so both grew without bound while
    `/ops/logs/` and `activity.json` scanned them on every poll.

    Retention comes from `runtime_settings.get_audit_retention_days()`
    (env `AUDIT_RETENTION_DAYS`, DB-overridable), which previously governed
    a prune that did not exist.

    Running SdkOperation rows (`ok=None`) are never pruned by age alone —
    an unfinished row past the window is the evidence that a worker died
    mid-run, and `/ops/tasks/` flags it as stale.
    """
    from django.utils import timezone

    from apps.core.runtime_settings import get_audit_retention_days
    from apps.events.models import Event, SdkOperation

    days = get_audit_retention_days()
    cutoff = timezone.now() - timezone.timedelta(days=days)

    events, _ = Event.objects.filter(created_at__lt=cutoff).delete()
    ops, _ = SdkOperation.objects.filter(created_at__lt=cutoff, ok__isnull=False).delete()

    log.info(
        "prune_event_log: retention_days=%s events=%s sdk_operations=%s",
        days,
        events,
        ops,
    )
    return {"retention_days": days, "events": events, "sdk_operations": ops}


__all__ = ["run_prune_event_log"]
