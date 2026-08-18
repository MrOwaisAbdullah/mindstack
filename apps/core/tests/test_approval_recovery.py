"""An approval that never reported back must be recoverable.

Q2 on the Redis broker has no ack. A worker killed mid-apply loses the
task outright, and because every ops action requires `pending`, the Feed
becomes unreachable: it cannot be approved, rejected, edited or retried,
ever. There was no scheduled job, no button, and no command that could
move it — the only exit was editing the database by hand.

The worst shape is a crash between a successful `git push` and
`feed.save`: the commit is in the brain and being served, while the DB
says `approving` with an empty `commit_hash`, permanently, and nothing
anywhere looked at the `Feed-Id:` trailer that would settle it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from apps.brainconfig import maintenance
from apps.feeds.models import Feed
from apps.feeds.services import approval

from .conftest import index_line, note

pytestmark = pytest.mark.django_db

NOTE_PATH = "knowledge/takes/a-take.md"


def _proposal() -> dict:
    return {"files": [note("a-take")], "index_lines": [index_line("a-take")]}


def _stuck(*, age_minutes: int = 120, **kw) -> Feed:
    return Feed.objects.create(
        source_id="src-1",
        channel="ui",
        status="approving",
        raw_payload={"source_kind": "blog"},
        proposal=_proposal(),
        approve_claimed_at=timezone.now() - timedelta(minutes=age_minutes),
        **kw,
    )


class TestApprovalInFlight:
    def test_a_fresh_claim_is_in_flight(self):
        assert _stuck(age_minutes=0).approval_in_flight is True

    def test_a_claim_past_the_horizon_is_not(self):
        horizon = int(settings.Q_CLUSTER["timeout"]) + 5 * 60
        feed = _stuck(age_minutes=int(horizon / 60) + 5)
        assert feed.approval_in_flight is False

    def test_a_claim_with_no_timestamp_is_recoverable(self):
        """Claimed before the field existed. The alternative is a feed
        that can never be recovered, which is the bug itself."""
        feed = _stuck()
        feed.approve_claimed_at = None
        assert feed.approval_in_flight is False

    def test_a_feed_that_is_not_approving_is_never_in_flight(self):
        feed = _stuck(age_minutes=0)
        feed.status = "approved"
        assert feed.approval_in_flight is False


class TestTheCrashAfterPush:
    """The worst case: the commit landed, the DB never heard."""

    @pytest.fixture()
    def pushed_but_unrecorded(self, brain, monkeypatch):
        """Run a real approval, then die immediately after the push."""
        feed = _stuck()

        real_push = approval._push

        def push_then_die(branch: str) -> None:
            real_push(branch)
            raise KeyboardInterrupt("worker killed")

        monkeypatch.setattr(approval, "_push", push_then_die)
        with pytest.raises(KeyboardInterrupt):
            approval.apply_feed(feed.pk)
        monkeypatch.undo()

        feed.refresh_from_db()
        assert feed.status == "approving"
        assert feed.commit_hash == ""
        return feed

    def test_the_commit_really_is_in_the_brain(self, brain, pushed_but_unrecorded):
        assert "> VERBATIM:" in brain.upstream_text(NOTE_PATH)

    def test_reconcile_completes_the_feed_against_that_commit(
        self, brain, pushed_but_unrecorded
    ):
        result = approval.reconcile_stuck()

        assert result == {"scanned": 1, "recovered": 1, "returned": 0, "skipped": 0}
        feed = pushed_but_unrecorded
        feed.refresh_from_db()
        assert feed.status == "approved"
        assert feed.commit_hash == brain.origin_head()
        assert feed.decided_at is not None
        assert feed.approve_claimed_at is None

    def test_it_does_not_commit_the_proposal_a_second_time(
        self, brain, pushed_but_unrecorded
    ):
        approval.reconcile_stuck()
        assert brain.log_subjects().count("feed: src-1") == 1

    def test_an_edited_proposal_is_recovered_as_edited(self, brain, monkeypatch):
        feed = _stuck(proposal_edited=True)
        real_push = approval._push
        monkeypatch.setattr(
            approval,
            "_push",
            lambda b: (real_push(b), (_ for _ in ()).throw(KeyboardInterrupt()))[0],
        )
        with pytest.raises(KeyboardInterrupt):
            approval.apply_feed(feed.pk)
        monkeypatch.undo()

        approval.reconcile_stuck()

        feed.refresh_from_db()
        assert feed.status == "edited"


class TestTheTaskThatNeverRan:
    def test_a_feed_with_no_commit_goes_back_to_pending(self, brain):
        feed = _stuck()

        result = approval.reconcile_stuck()

        assert result == {"scanned": 1, "recovered": 0, "returned": 1, "skipped": 0}
        feed.refresh_from_db()
        assert feed.status == "pending"
        assert feed.commit_hash == ""
        assert feed.decided_at is None
        assert feed.approve_claimed_at is None

    def test_the_error_explains_that_nothing_was_committed(self, brain):
        feed = _stuck()
        approval.reconcile_stuck()

        feed.refresh_from_db()
        assert "never reported back" in feed.error
        assert "Nothing was committed" in feed.error

    def test_the_proposal_survives_so_it_can_be_re_approved(self, brain):
        feed = _stuck()
        approval.reconcile_stuck()

        feed.refresh_from_db()
        assert feed.proposal == _proposal()

        Feed.objects.filter(pk=feed.pk).update(status="approving")
        approval.apply_feed(feed.pk)
        feed.refresh_from_db()
        assert feed.status == "approved", feed.error


class TestItLeavesLiveWorkAlone:
    def test_a_fresh_claim_is_not_touched(self, brain):
        feed = _stuck(age_minutes=0)

        assert approval.reconcile_stuck()["scanned"] == 0

        feed.refresh_from_db()
        assert feed.status == "approving"

    def test_force_reaches_a_fresh_claim(self, brain):
        feed = _stuck(age_minutes=0)

        assert approval.reconcile_stuck(force=True)["scanned"] == 1

        feed.refresh_from_db()
        assert feed.status == "pending"

    def test_a_worker_that_finishes_first_wins(self, brain, monkeypatch):
        """Every write is a conditional `status='approving'` UPDATE, so a
        live worker's result is never overwritten.

        The race is between the scan and the write, so the steal happens
        inside the trailer lookup — that is where the window actually is.
        """
        feed = _stuck()

        def steal(pk, branch):
            Feed.objects.filter(pk=pk).update(status="approved", commit_hash="deadbeef")
            return ""

        monkeypatch.setattr(approval, "landed_commit", steal)

        result = approval.reconcile_stuck()

        assert result["skipped"] == 1
        feed.refresh_from_db()
        assert feed.status == "approved"
        assert feed.commit_hash == "deadbeef"

    def test_a_feed_that_is_not_approving_is_not_scanned(self, brain):
        feed = _stuck()
        Feed.objects.filter(pk=feed.pk).update(status="pending")
        assert approval.reconcile_stuck()["scanned"] == 0

    def test_no_stuck_feeds_means_no_git_call(self, brain, monkeypatch):
        monkeypatch.setattr(
            approval.gitrepo,
            "run",
            lambda *a, **k: pytest.fail(f"touched git for nothing: {a}"),
        )
        assert approval.reconcile_stuck() == {
            "scanned": 0, "recovered": 0, "returned": 0, "skipped": 0
        }


class TestMixedBatch:
    def test_each_feed_is_judged_on_its_own_trailer(self, brain, monkeypatch):
        landed = _stuck()
        real_push = approval._push
        monkeypatch.setattr(
            approval,
            "_push",
            lambda b: (real_push(b), (_ for _ in ()).throw(KeyboardInterrupt()))[0],
        )
        with pytest.raises(KeyboardInterrupt):
            approval.apply_feed(landed.pk)
        monkeypatch.undo()

        never_ran = _stuck()

        result = approval.reconcile_stuck()

        assert (result["recovered"], result["returned"]) == (1, 1)
        landed.refresh_from_db()
        never_ran.refresh_from_db()
        assert landed.status == "approved"
        assert never_ran.status == "pending"


class TestTheOperatorSurfaceExists:
    """The finding was "no recovery path AT ALL" — so the wiring is the
    fix, and an inert service would pass every behavioural test above."""

    def test_a_scheduled_beat_is_declared(self):
        from config.scheduled import SCHEDULED_TASKS

        entry = next(
            (t for t in SCHEDULED_TASKS if t.name == "feeds:reconcile-approvals"), None
        )
        assert entry is not None
        assert entry.func == "apps.feeds.scheduled.run_reconcile_approvals"

    def test_the_scheduled_callable_is_importable_by_that_path(self):
        from django.utils.module_loading import import_string

        from config.scheduled import SCHEDULED_TASKS

        for task in SCHEDULED_TASKS:
            assert callable(import_string(task.func)), task.name

    def test_the_job_appears_on_the_tasks_page(self):
        assert maintenance.RECONCILE_APPROVALS in maintenance.ALL_JOBS

    def test_the_job_task_path_resolves(self):
        from django.utils.module_loading import import_string

        assert callable(import_string(maintenance.RECONCILE_APPROVALS.task))

    def test_the_feed_page_offers_recovery(self):
        from pathlib import Path

        from django.conf import settings as dj

        html = (Path(dj.BASE_DIR) / "templates/ops/feed_detail.html").read_text(
            encoding="utf-8"
        )
        assert 'value="recover"' in html
        assert "approval_in_flight" in html

    def test_the_management_command_exists(self):
        from django.core.management import get_commands

        assert get_commands().get("reconcile_feeds") == "apps.feeds"


class TestTheOpsAction:
    def test_recover_enqueues_the_job(self, brain, monkeypatch):
        from apps.brainconfig import jobs

        feed = _stuck()
        seen = []
        monkeypatch.setattr(jobs, "enqueue", lambda spec: (seen.append(spec), (True, "ok"))[1])

        _post(feed, "recover")

        assert seen == [maintenance.RECONCILE_APPROVALS]

    def test_recover_refuses_while_the_approval_is_in_flight(self, brain, monkeypatch):
        from apps.brainconfig import jobs

        feed = _stuck(age_minutes=0)
        seen = []
        monkeypatch.setattr(jobs, "enqueue", lambda spec: (seen.append(spec), (True, "ok"))[1])

        _post(feed, "recover")

        assert seen == []

    def test_recover_refuses_on_a_feed_that_is_not_approving(self, brain, monkeypatch):
        from apps.brainconfig import jobs

        feed = _stuck()
        Feed.objects.filter(pk=feed.pk).update(status="pending")
        feed.refresh_from_db()
        seen = []
        monkeypatch.setattr(jobs, "enqueue", lambda spec: (seen.append(spec), (True, "ok"))[1])

        _post(feed, "recover")

        assert seen == []

    def test_approving_stamps_the_claim(self, brain, monkeypatch):
        """Without the timestamp nothing can tell a live approval from a
        lost one, and the whole recovery collapses."""
        import apps.feeds.ops_views as ops_views

        monkeypatch.setattr(ops_views.validator, "validate_feed", lambda f: _Valid())
        monkeypatch.setitem(
            __import__("sys").modules, "django_q.tasks", _FakeTasks()
        )
        feed = Feed.objects.create(
            source_id="s", channel="ui", status="pending",
            raw_payload={"source_kind": "blog"}, proposal=_proposal(),
        )

        _post(feed, "approve")

        feed.refresh_from_db()
        assert feed.status == "approving"
        assert feed.approve_claimed_at is not None
        assert feed.approval_in_flight is True


# ---- helpers -------------------------------------------------------------


class _Valid:
    valid = True
    violations: list = []


class _FakeTasks:
    @staticmethod
    def async_task(*a, **kw):
        return "task-id"


def _post(feed: Feed, action: str):
    """Drive `_handle_action` directly — the view's auth and rendering are
    not what these tests are about."""
    from django.contrib.messages.storage.base import BaseStorage
    from django.test import RequestFactory

    import apps.feeds.ops_views as ops_views

    request = RequestFactory().post(f"/ops/feeds/{feed.pk}/", {"action": action})
    request._messages = _NullMessages(request)
    ops_views._handle_action(request, feed)
    return request


class _NullMessages:
    def __init__(self, request):
        self.request = request
        self.messages = []

    def add(self, level, message, extra_tags=""):
        self.messages.append((level, str(message)))
