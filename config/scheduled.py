"""Declared Q2 cron schedule — the single source of truth.

`manage.py sync_scheduled` reconciles `django_q.models.Schedule` rows to
this list (create / update in place / prune orphans), and the entrypoint
runs it on every web boot, so a deploy always converges the schedule.

This file did not exist. `sync_scheduled` imported it and died with
ModuleNotFoundError, so NO schedule row was ever created on any install
and everything below simply never ran: idempotency keys were never purged
(completed responses replayed forever, and a stranded in-flight row 409'd
that key indefinitely instead of clearing at 24h), SDK transcripts holding
plaintext note content accumulated without bound, the event log and token
ledger grew forever with no prune path at all, and the brain was only ever
re-synced when a webhook happened to fire — while several failure paths in
the sync and approval code logged "the sync beat will repair it".

Adding a task:
  1. write a top-level callable in `<app>/scheduled.py` (Q2 imports it by
     dotted path from a fresh worker process — it must not close over
     request state, and it should be safe to run twice)
  2. declare it here
  3. `manage.py sync_scheduled` (the entrypoint does this for you on the
     next deploy)

Times are UTC, staggered so the beats don't pile onto one minute.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScheduledTask:
    #: Stable identity. Renaming this orphans the old Schedule row, which
    #: `sync_scheduled --prune` then removes.
    name: str
    #: Dotted path to a top-level callable.
    func: str
    #: Crontab expression, UTC.
    cron: str
    #: "C" = Schedule.CRON.
    schedule_type: str = "C"
    kwargs: dict = field(default_factory=dict)


SCHEDULED_TASKS: list[ScheduledTask] = [
    # Pull, reindex, rebuild snapshots. The GitHub webhook is the fast
    # path; this is the backstop for when it is unconfigured, failing, or
    # the repo was pushed from somewhere that doesn't notify us. Cheap
    # when there's nothing to do: no new commits means no reindex.
    ScheduledTask(
        name="brain:sync",
        func="apps.brain.scheduled.run_sync_brain",
        cron="*/15 * * * *",
    ),
    # The 24h idempotency TTL is a contract the API documents; this is the
    # only thing that enforces it.
    ScheduledTask(
        name="core:purge-idempotency-keys",
        func="apps.core.scheduled.run_purge_idempotency_keys",
        cron="17 * * * *",
    ),
    # Plaintext note content in CLI session files. Retention, not
    # immortality.
    ScheduledTask(
        name="reader:cleanup-sdk-transcripts",
        func="apps.reader.scheduled.run_cleanup_sdk_transcripts",
        cron="23 3 * * *",
    ),
    # Event log + finished SDK ledger rows, per AUDIT_RETENTION_DAYS.
    ScheduledTask(
        name="events:prune-log",
        func="apps.events.scheduled.run_prune_event_log",
        cron="41 3 * * *",
    ),
    # Q2 on the Redis broker has no ack, so a worker killed mid-approval
    # loses the task and the Feed sits in `approving` forever — a status
    # every ops action refuses. That happens during a deploy, which is
    # exactly when nobody is watching the feed queue. Cheap when there is
    # nothing to do: no stuck feeds means no git call at all.
    ScheduledTask(
        name="feeds:reconcile-approvals",
        func="apps.feeds.scheduled.run_reconcile_approvals",
        cron="4-59/10 * * * *",
    ),
]

__all__ = ["ScheduledTask", "SCHEDULED_TASKS"]
