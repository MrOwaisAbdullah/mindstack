"""A failed apply must never leave files in the clone.

`apply_feed` caught only `ApplyFailure` and `BrainRepoError`. Everything
else — an `OSError` from `mkdir` where a path component is an existing
file, a `UnicodeDecodeError` from one of the strict `read_text` calls, a
DB blip inside `context_from_repo` — escaped the inner handler with the
proposal's files already written to the working tree and no rollback.

That is not a cosmetic leak. `indexer.rebuild()` parses the WORKING TREE,
so the leftovers get indexed, copied into tier snapshots, and served over
the public API, having never been part of any commit a human approved.
`pull --rebase --autostash` on the next sync preserves them rather than
clearing them.

The escape also left the Feed itself stuck in `approving`, which no ops
UI action can act on.
"""
from __future__ import annotations

import pytest

from apps.feeds.models import Feed
from apps.feeds.services import approval

from .conftest import raw_file

pytestmark = pytest.mark.django_db


def _feed(**proposal) -> Feed:
    return Feed.objects.create(
        source_id="src-1",
        channel="ui",
        status="approving",
        raw_payload={"source_kind": "blog"},
        proposal=proposal or {"files": [raw_file()]},
    )


def _explode(monkeypatch, exc: BaseException) -> None:
    """Blow up at the last apply step, after files are already written."""

    def boom(repo, proposal):
        raise exc

    monkeypatch.setattr(approval, "_apply_taxonomy", boom)


class TestTheTreeComesBackClean:
    def test_arbitrary_exception_rolls_back(self, brain, monkeypatch):
        feed = _feed(files=[raw_file("raw/one.md"), raw_file("raw/two.md")])
        _explode(monkeypatch, RuntimeError("something nobody predicted"))

        approval.apply_feed(feed.pk)

        assert brain.dirty() == ""
        assert not (brain.clone / "raw" / "one.md").exists()
        assert not (brain.clone / "raw" / "two.md").exists()

    def test_os_error_from_a_path_component_that_is_a_file(self, brain):
        """The finding's own example, with no monkeypatching at all.

        `raw/notes.md` exists as a file, so `mkdir` for the parent of
        `raw/notes.md/extra.md` raises OSError — after `raw/first.md` has
        already been written.
        """
        (brain.clone / "raw" / "notes.md").write_text("existing\n", encoding="utf-8")
        brain.git("add", "-A")
        brain.git("commit", "-m", "add a raw note")
        brain.git("push", "origin", "main")

        feed = _feed(files=[raw_file("raw/first.md"), raw_file("raw/notes.md/extra.md")])
        approval.apply_feed(feed.pk)

        assert brain.dirty() == ""
        assert not (brain.clone / "raw" / "first.md").exists()

    def test_the_feed_is_marked_failed_not_left_approving(self, brain, monkeypatch):
        feed = _feed()
        _explode(monkeypatch, RuntimeError("boom"))

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "failed"
        assert feed.commit_hash == ""

    def test_the_error_names_the_exception_type(self, brain, monkeypatch):
        feed = _feed()
        _explode(monkeypatch, ValueError("bad value"))

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert "ValueError" in feed.error and "bad value" in feed.error

    def test_nothing_reaches_the_origin(self, brain, monkeypatch):
        before = brain.origin_head()
        feed = _feed()
        _explode(monkeypatch, RuntimeError("boom"))

        approval.apply_feed(feed.pk)

        assert brain.origin_head() == before
        assert brain.head() == before


class TestCancellationStillCleansUp:
    def test_base_exception_propagates_but_rolls_back(self, brain, monkeypatch):
        """A worker shutdown mid-apply is not a Feed failure to record —
        but the tree must not keep half a proposal either."""
        feed = _feed(files=[raw_file("raw/one.md")])
        _explode(monkeypatch, KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            approval.apply_feed(feed.pk)

        assert brain.dirty() == ""
        assert not (brain.clone / "raw" / "one.md").exists()


class TestAnIncompleteRollbackIsReported:
    def test_leftovers_are_named_in_the_feed_error(self, brain, monkeypatch):
        """If `reset`/`clean` cannot clear the tree, silence is the worst
        outcome — the next sync serves whatever is left."""
        real_run = approval.gitrepo.run
        applied = False

        def boom(repo, proposal):
            nonlocal applied
            applied = True
            raise RuntimeError("boom")

        def refuse_cleanup(*args, **kw):
            # Only once the files are on disk — the attempt loop's own
            # opening `reset` has to keep working.
            if applied and args[:1] in (("reset",), ("clean",)):
                raise approval.gitrepo.BrainRepoError("permission denied")
            return real_run(*args, **kw)

        feed = _feed(files=[raw_file("raw/one.md")])
        monkeypatch.setattr(approval, "_apply_taxonomy", boom)
        monkeypatch.setattr(approval.gitrepo, "run", refuse_cleanup)

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert "ROLLBACK INCOMPLETE" in feed.error
        assert "raw/one.md" in feed.error


class TestTheHappyPathStillWorks:
    def test_a_valid_proposal_commits_and_pushes(self, brain):
        feed = _feed()

        result = approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", result
        assert feed.commit_hash and feed.error == ""
        assert brain.origin_head() == feed.commit_hash
        assert brain.upstream_text("raw/capture.md") == "captured text"
        assert brain.dirty() == ""

    def test_the_commit_carries_the_feed_trailer(self, brain):
        feed = _feed()
        approval.apply_feed(feed.pk)

        message = brain.git("log", "-1", "--format=%B")
        assert f"feed: {feed.source_id}" in message
        assert f"Feed-Id: {feed.pk}" in message
