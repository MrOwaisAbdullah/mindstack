"""`manage.py sync_scheduled` — reconcile Q2 Schedule rows to config/scheduled.py.

Idempotent: reads `config.scheduled.SCHEDULED_TASKS`, compares to
`django_q.models.Schedule` rows, and:

  - **creates** a Schedule row for every declared task missing from the DB
  - **updates in place** when the DB row's func / cron / kwargs differ
  - **deletes** orphan Schedule rows whose `name` is no longer in the
    declared list (under `--prune`; default leaves them alone so an
    operator-created Schedule row isn't silently removed)

Print the diff before applying. `--dry-run` skips the writes.

Run from CI post-deploy (or from a one-off `manage.py` shell) so a
fresh worker process picks up the right schedule. The Q2 cluster's
scheduler thread polls every 30s, so the change takes effect within
that window.
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from config.scheduled import SCHEDULED_TASKS, ScheduledTask


class Command(BaseCommand):
    help = "Reconcile django_q.models.Schedule rows to match config/scheduled.py (idempotent)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the diff without applying.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help=(
                "Delete Schedule rows whose `name` matches a declared task's "
                "name pattern but is no longer in the declaration list. "
                "OFF by default so operator-created Schedule rows aren't "
                "silently removed."
            ),
        )

    def handle(self, *args: Any, dry_run: bool, prune: bool, **options: Any) -> None:
        from django_q.models import Schedule

        declared: dict[str, ScheduledTask] = {t.name: t for t in SCHEDULED_TASKS}
        existing_qs = Schedule.objects.filter(name__in=list(declared.keys()))
        existing_by_name = {s.name: s for s in existing_qs}

        creates: list[str] = []
        updates: list[str] = []
        unchanged: list[str] = []

        for task in SCHEDULED_TASKS:
            current = existing_by_name.get(task.name)
            if current is None:
                creates.append(task.name)
                if not dry_run:
                    Schedule.objects.create(**_schedule_kwargs(task))
                continue
            diff = _diff_schedule_row(current, task)
            if diff:
                updates.append(f"{task.name} ({', '.join(diff)})")
                if not dry_run:
                    for field, value in _schedule_kwargs(task).items():
                        setattr(current, field, value)
                    current.save()
            else:
                unchanged.append(task.name)

        prunes: list[str] = []
        if prune:
            # Only rows this file owns. The old query was
            # `.exclude(declared).filter(name__startswith="")` — i.e.
            # EVERY cron row not currently declared, which silently deleted
            # an operator's hand-created Schedule despite the docstring
            # promising the opposite. Declared names are namespaced
            # `<app>:<task>`, so ownership is decidable: prune a row only
            # if its namespace is one we declare but its full name is not.
            owned_prefixes = {n.split(":", 1)[0] + ":" for n in declared if ":" in n}
            orphan_qs = Schedule.objects.exclude(name__in=list(declared.keys()))
            for row in orphan_qs:
                if not any(row.name.startswith(p) for p in owned_prefixes):
                    continue
                # Don't prune Schedule.ONCE rows -- those are one-off
                # delivery retries the webhooks app creates ad hoc, not
                # cron entries. They self-destruct on completion.
                if row.schedule_type == "O":  # Schedule.ONCE
                    continue
                prunes.append(row.name)
                if not dry_run:
                    row.delete()

        # ---- Report -------------------------------------------------------
        action_word = "Would" if dry_run else "Did"
        self.stdout.write(f"{action_word} create: {len(creates)} row(s)")
        for n in creates:
            self.stdout.write(f"  + {n}")
        self.stdout.write(f"{action_word} update: {len(updates)} row(s)")
        for n in updates:
            self.stdout.write(f"  ~ {n}")
        if prune:
            self.stdout.write(f"{action_word} prune: {len(prunes)} orphan row(s)")
            for n in prunes:
                self.stdout.write(f"  - {n}")
        self.stdout.write(f"Unchanged: {len(unchanged)} row(s)")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes."))
        else:
            self.stdout.write(self.style.SUCCESS("sync_scheduled OK."))


def _schedule_kwargs(task: ScheduledTask) -> dict[str, Any]:
    """Build the kwargs `Schedule.objects.create(**...)` expects.

    Django-Q2 uses `cron` for the crontab expression and accepts
    `schedule_type="C"` (Schedule.CRON). `kwargs` is serialized via the
    Schedule model's `kwargs` field (JSON-stored pickle). `repeats=-1`
    means "run indefinitely".
    """
    return {
        "name": task.name,
        "func": task.func,
        "cron": task.cron,
        "schedule_type": task.schedule_type,
        "repeats": -1,
        "kwargs": _kwargs_literal(task.kwargs),
    }


def _kwargs_literal(kwargs: dict) -> str:
    """Serialize kwargs the way django-q2 actually reads them back.

    Q2's scheduler parses `Schedule.kwargs` with `ast.literal_eval` and, on
    failure, falls back to keyword-syntax parsing and then to `{}` —
    SILENTLY. JSON only round-trips by accident: `{"a": 1}` happens to be a
    valid Python literal, but `true`/`false`/`null` are not, so a task
    declared with `kwargs={"dry_run": False}` synced cleanly, diffed as
    unchanged forever, and ran with no kwargs at all.

    `repr()` of a dict is a Python literal by construction.
    """
    return repr(kwargs) if kwargs else ""


def _diff_schedule_row(row: Any, task: ScheduledTask) -> list[str]:
    """Return human-readable list of fields that differ between the DB
    row and the declared task. Empty list = no drift."""
    declared_kwargs_json = _kwargs_literal(task.kwargs)
    diffs = []
    if row.func != task.func:
        diffs.append(f"func: {row.func} -> {task.func}")
    if row.cron != task.cron:
        diffs.append(f"cron: {row.cron!r} -> {task.cron!r}")
    if row.schedule_type != task.schedule_type:
        diffs.append(f"type: {row.schedule_type} -> {task.schedule_type}")
    if (row.kwargs or "") != declared_kwargs_json:
        diffs.append("kwargs")
    if row.repeats != -1:
        diffs.append(f"repeats: {row.repeats} -> -1")
    return diffs
