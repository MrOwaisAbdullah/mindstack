"""Named background jobs the ops UI can start, watch, and report on.

The setup Build step needed a progress record; the repair actions need
the same thing, so it lives here once rather than twice. A job is just a
cache-backed record — `{state, label, step, total, error, …}` — that a Q2
task updates as it goes.

Why the cache and not a table: progress is ephemeral by definition, and
losing Redis should cost a progress bar, not a migration. The *outcome*
of anything that matters is already durable elsewhere (the Entity index,
the clone, an Event row) — this record only answers "is it running, and
what is it doing".

Two rules the ops UI depends on:

- **Every exit path writes a terminal state.** A job that dies silently
  leaves a spinner forever, which reads as "still working" when it
  actually means "nobody is coming". `run()` guarantees this.
- **A job that is running cannot be started again.** django_q is happy to
  run two clones over the same directory; the repo lock would serialise
  them, but the second is pure waste and the UI would misreport both.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from django.core.cache import cache

log = logging.getLogger(__name__)

CACHE_PREFIX = "opsjob:"
TTL_SECONDS = 60 * 30


@dataclass(frozen=True)
class JobSpec:
    name: str
    label: str
    #: Dotted path run on the worker. Must be importable there.
    task: str
    #: Shown on the confirm prompt when the action destroys something.
    danger: str = ""


def _key(name: str) -> str:
    return f"{CACHE_PREFIX}{name}"


def get(name: str) -> dict:
    return cache.get(_key(name)) or {}


def update(name: str, **fields) -> None:
    current = get(name)
    current.update(fields)
    cache.set(_key(name), current, TTL_SECONDS)


def clear(name: str) -> None:
    cache.delete(_key(name))


def is_running(name: str) -> bool:
    return get(name).get("state") == "running"


def mark_queued(name: str, label: str) -> None:
    """Write the running state in the REQUEST, not the worker.

    If the worker is slow to pick up (or down), the UI still shows the
    job as started rather than appearing to have ignored the click.

    Replaces the record rather than merging into it. A job body may stash
    its own fields (the read check stores git's stderr and the missing
    contract files, which `run()`'s label/error pair cannot carry), and
    merging would leave the previous run's verdict sitting on this run's
    record — visible for as long as the new one takes.
    """
    cache.set(
        _key(name),
        {"state": "running", "step": 0, "total": 1, "label": label,
         "error": "", "result": ""},
        TTL_SECONDS,
    )


def enqueue(spec: JobSpec) -> tuple[bool, str]:
    """Start a job. Returns (started, message)."""
    if is_running(spec.name):
        return False, f"{spec.label} is already running."
    mark_queued(spec.name, f"{spec.label}…")
    try:
        from django_q.tasks import async_task

        async_task(spec.task)
    except Exception as exc:
        update(spec.name, state="failed", label="Could not reach the worker queue",
               error=str(exc))
        return False, f"Could not reach the worker queue: {exc}"
    return True, f"{spec.label} started."


def run(name: str, label: str, fn: Callable[[Callable[[int, int, str], None]], str]) -> None:
    """Execute a job body on the worker, guaranteeing a terminal state.

    `fn` receives a `progress(step, total, text)` callback and returns a
    short result string for the UI.
    """
    def progress(step: int, total: int, text: str) -> None:
        update(name, state="running", step=step, total=total, label=text, error="")

    update(name, state="running", step=0, total=1, label=f"{label}…", error="")
    try:
        result = fn(progress)
        update(name, state="done", step=1, total=1, label=label,
               result=result or "Done.", error="")
        log.info("ops job %s finished: %s", name, result)
    except Exception as exc:
        log.exception("ops job %s failed", name)
        update(name, state="failed", label=f"{label} failed",
               error=f"{exc.__class__.__name__}: {exc}", result="")


def active() -> list[dict]:
    """Every job with a record, newest states first — for the Tasks page."""
    from apps.brainconfig import maintenance

    out = []
    for spec in maintenance.ALL_JOBS:
        record = get(spec.name)
        if record:
            out.append({"spec": spec, **record})
    return out
