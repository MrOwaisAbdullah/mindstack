"""A reviewed diff must not be applied onto a brain that has moved.

Every attempt starts with `reset --hard origin/<branch>` and re-applies
FULL file contents. That is what makes push-race replay safe for the
proposal — and what silently reverted anything upstream had done to the
same file in between. The operator approved a diff `diffview.build`
computed against the clone at review time; the commit that landed was a
different one, and nothing anywhere said so.

The finding names the push-race replay, but the same window is open
without any race: the clone advances on every sync, so a brain edit
pushed between the review and the worker's run is reverted by the very
first attempt.

The outcome is `pending`, not `failed` — the proposal is fine, the
review is stale, and `pending` is the only status the ops UI can act on.
"""
from __future__ import annotations

import pytest

from apps.feeds.models import Feed
from apps.feeds.services import approval

from .conftest import index_line, note, raw_file

pytestmark = pytest.mark.django_db

NOTE_PATH = "knowledge/takes/a-take.md"


def _feed(proposal: dict | None = None) -> Feed:
    return Feed.objects.create(
        source_id="src-1",
        channel="ui",
        status="approving",
        raw_payload={"source_kind": "blog"},
        proposal=proposal or {"files": [note("a-take")], "index_lines": [index_line("a-take")]},
    )


@pytest.fixture()
def race(monkeypatch):
    """Turn the first push into a genuine non-fast-forward rejection.

    Someone else's commit lands on the origin between this attempt's
    `reset` and its `push`, which is exactly the race the retry loop
    exists for.
    """

    def arm(brain, path: str, content: str):
        real_push = approval._push
        pushed: list[int] = []

        def racing_push(branch: str) -> None:
            if not pushed:
                pushed.append(1)
                brain.push_upstream(path, content)
            return real_push(branch)

        monkeypatch.setattr(approval, "_push", racing_push)

    return arm


class TestUpstreamEditsSurviveAPushRace:
    def test_an_overlapping_race_stops_instead_of_reverting(self, brain, race):
        """The finding. Unfixed, this feed is approved and the upstream
        edit to the same file is gone from the brain."""
        race(brain, NOTE_PATH, "---\nid: a-take\n---\n\nUPSTREAM WROTE THIS\n")
        feed = _feed()

        approval.apply_feed(feed.pk)

        assert "UPSTREAM WROTE THIS" in brain.upstream_text(NOTE_PATH)
        feed.refresh_from_db()
        assert feed.status == "pending"

    def test_a_non_overlapping_race_still_replays_and_lands(self, brain, race):
        """Replay is the correct behaviour when nothing collides — the
        fix must not turn every race into a stall."""
        race(brain, "raw/unrelated.md", "someone else's capture\n")
        feed = _feed()

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert feed.retries == 1
        assert "someone else's capture" in brain.upstream_text("raw/unrelated.md")
        assert "id: a-take" in brain.upstream_text(NOTE_PATH)


class TestTheSameWindowWithoutARace:
    def test_an_edit_pushed_before_the_worker_runs(self, brain):
        """No push race at all: the clone is simply behind the origin."""
        brain.push_upstream(NOTE_PATH, "---\nid: a-take\n---\n\nUPSTREAM WROTE THIS\n")
        feed = _feed()

        approval.apply_feed(feed.pk)

        assert "UPSTREAM WROTE THIS" in brain.upstream_text(NOTE_PATH)
        feed.refresh_from_db()
        assert feed.status == "pending"

    def test_an_unrelated_upstream_edit_does_not_block(self, brain):
        brain.push_upstream("raw/unrelated.md", "someone else's capture\n")
        feed = _feed()

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert "someone else's capture" in brain.upstream_text("raw/unrelated.md")


class TestTheOperatorCanRecover:
    def test_the_feed_is_actionable_again(self, brain):
        brain.push_upstream(NOTE_PATH, "---\nid: a-take\n---\n\nUPSTREAM\n")
        feed = _feed()
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        # `pending` is the only status `_handle_action` will act on.
        assert feed.status == "pending"
        assert feed.decided_at is None
        assert feed.commit_hash == ""

    def test_the_error_names_the_file_that_moved(self, brain):
        brain.push_upstream(NOTE_PATH, "---\nid: a-take\n---\n\nUPSTREAM\n")
        feed = _feed()
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert NOTE_PATH in feed.error
        assert "approve again" in feed.error

    def test_re_approving_after_the_clone_catches_up_works(self, brain):
        """Once the operator has looked at the new diff, the second
        approval starts from the current clone and lands normally."""
        brain.push_upstream(NOTE_PATH, "---\nid: a-take\n---\n\nUPSTREAM\n")
        feed = _feed()
        approval.apply_feed(feed.pk)

        # Re-opening the feed is what syncs the clone in production; here
        # the pull stands in for it.
        brain.git("pull", "--ff-only")
        Feed.objects.filter(pk=feed.pk).update(status="approving")
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert "> VERBATIM:" in brain.upstream_text(NOTE_PATH)

    def test_an_event_records_the_stall(self, brain):
        from apps.events.models import Event

        brain.push_upstream(NOTE_PATH, "---\nid: a-take\n---\n\nUPSTREAM\n")
        approval.apply_feed(_feed().pk)

        ev = next(
            (e for e in Event.objects.all() if e.details.get("action") == "stale_review"), None
        )
        assert ev is not None
        assert NOTE_PATH in ev.details["paths"]


class TestMergedFilesAreNotTreatedAsCollisions:
    """INDEX.md and CLAUDE.md are re-read and merged, not replaced, so an
    upstream change to either must not stall the approval."""

    def test_an_upstream_index_edit_is_kept_and_merged(self, brain):
        brain.push_upstream(
            "INDEX.md",
            "# INDEX\n\n## Knowledge\n- [fact] Upstream — added elsewhere | status: current | knowledge/facts/u.md\n\n## Projects\n\n## Content\n",
        )
        feed = _feed()

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        committed = brain.upstream_text("INDEX.md")
        assert "added elsewhere" in committed
        assert NOTE_PATH in committed

    def test_an_upstream_claude_md_edit_is_kept(self, brain):
        brain.push_upstream(
            "CLAUDE.md", "# Brain\n\n## Topic taxonomy\n\nTags: `ai, writing, systems, newtag`\n"
        )
        feed = _feed()

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert "newtag" in brain.upstream_text("CLAUDE.md")


class TestNoChangeToTheOrdinaryPath:
    def test_a_clean_apply_is_unaffected(self, brain):
        feed = _feed({"files": [raw_file()]})

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert feed.retries == 0

    def test_a_proposal_with_no_files_does_not_probe_git(self, brain):
        feed = _feed({"files": [], "index_lines": []})

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        # Nothing to commit is a separate finding; what matters here is
        # that the staleness check did not fire on an empty path set.
        assert feed.status != "pending"
