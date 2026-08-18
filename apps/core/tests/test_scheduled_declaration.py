"""The cron schedule must actually exist and actually apply.

`config/scheduled.py` was imported by `sync_scheduled` but absent from the
repo, so the command died with ModuleNotFoundError and NO Schedule row was
ever created on any install: idempotency keys were never purged, SDK
transcripts holding plaintext note content grew forever, the event log and
token ledger had no prune path at all, and the brain was only re-synced
when a webhook happened to fire.

These tests pin the three things that made that failure invisible: the
module exists, every declared callable is importable by its dotted path,
and the reconcile is idempotent.
"""
from __future__ import annotations

import ast
import importlib
from io import StringIO

import pytest
from django.core.management import call_command

from apps.core.management.commands.sync_scheduled import _kwargs_literal
from config.scheduled import SCHEDULED_TASKS, ScheduledTask


def test_tasks_are_declared():
    assert SCHEDULED_TASKS, "an empty schedule means nothing is ever pruned or synced"


@pytest.mark.parametrize("task", SCHEDULED_TASKS, ids=lambda t: t.name)
def test_declared_callable_is_importable(task: ScheduledTask):
    """Q2 imports by dotted path from a fresh worker; a typo fails silently
    there, at 3am, in a container nobody is watching."""
    module_path, _, attr = task.func.rpartition(".")
    module = importlib.import_module(module_path)
    assert callable(getattr(module, attr)), f"{task.func} is not callable"


@pytest.mark.parametrize("task", SCHEDULED_TASKS, ids=lambda t: t.name)
def test_names_are_namespaced(task: ScheduledTask):
    """`--prune` decides ownership by namespace, so this is load-bearing."""
    assert ":" in task.name, f"{task.name!r} needs an <app>:<task> name"


def test_names_are_unique():
    names = [t.name for t in SCHEDULED_TASKS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("task", SCHEDULED_TASKS, ids=lambda t: t.name)
def test_cron_has_five_fields(task: ScheduledTask):
    assert len(task.cron.split()) == 5, f"{task.name}: {task.cron!r} is not a crontab line"


class TestKwargsLiteral:
    """Q2 reads Schedule.kwargs with ast.literal_eval and falls back to {}
    SILENTLY. JSON only round-trips by accident."""

    def test_empty_kwargs(self):
        assert _kwargs_literal({}) == ""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"days": 7},
            {"dry_run": False},
            {"enabled": True},
            {"opt": None},
            {"name": "x", "count": 2, "flag": True},
        ],
    )
    def test_round_trips_through_literal_eval(self, kwargs):
        """`{"dry_run": False}` as JSON is `false` — not a Python literal,
        so Q2 silently ran the task with no kwargs at all."""
        assert ast.literal_eval(_kwargs_literal(kwargs)) == kwargs


@pytest.mark.django_db
class TestSyncScheduled:
    def _rows(self):
        from django_q.models import Schedule

        return {s.name: s for s in Schedule.objects.all()}

    def test_creates_every_declared_task(self):
        call_command("sync_scheduled", stdout=StringIO())
        rows = self._rows()
        for task in SCHEDULED_TASKS:
            assert task.name in rows
            assert rows[task.name].func == task.func
            assert rows[task.name].cron == task.cron
            assert rows[task.name].repeats == -1

    def test_is_idempotent(self):
        call_command("sync_scheduled", stdout=StringIO())
        out = StringIO()
        call_command("sync_scheduled", stdout=out)
        assert f"Unchanged: {len(SCHEDULED_TASKS)} row(s)" in out.getvalue()

    def test_repairs_drift_in_place(self):
        call_command("sync_scheduled", stdout=StringIO())
        row = self._rows()[SCHEDULED_TASKS[0].name]
        row.cron = "0 0 31 2 *"
        row.save()

        call_command("sync_scheduled", stdout=StringIO())
        assert self._rows()[SCHEDULED_TASKS[0].name].cron == SCHEDULED_TASKS[0].cron

    def test_dry_run_writes_nothing(self):
        call_command("sync_scheduled", "--dry-run", stdout=StringIO())
        assert self._rows() == {}

    def test_prune_leaves_operator_rows_alone(self):
        """The docstring promised this; the query did the opposite."""
        from django_q.models import Schedule

        call_command("sync_scheduled", stdout=StringIO())
        Schedule.objects.create(
            name="my-nightly-thing", func="builtins.print", schedule_type="C", cron="0 4 * * *"
        )

        call_command("sync_scheduled", "--prune", stdout=StringIO())
        assert "my-nightly-thing" in self._rows()

    def test_prune_removes_our_own_orphans(self):
        from django_q.models import Schedule

        call_command("sync_scheduled", stdout=StringIO())
        Schedule.objects.create(
            name="core:retired-task", func="builtins.print", schedule_type="C", cron="0 4 * * *"
        )

        call_command("sync_scheduled", "--prune", stdout=StringIO())
        assert "core:retired-task" not in self._rows()
