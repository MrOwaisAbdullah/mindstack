"""A feed whose content is in the brain must not be marked `failed`.

A push can succeed on the server and still report failure to the client
— the ref moves, then the connection drops. The retry loop replays onto
a tree that already contains the proposal, `git commit` exits non-zero
with "nothing to commit", that reads as another push failure, and after
MAX_PUSH_ATTEMPTS the Feed is `failed` with an empty `commit_hash`
while its content is live in the brain and being served.

Every approval already writes a `Feed-Id: <pk>` trailer, and nothing
ever read it back. It is the only durable link between a Feed row and
the brain's history, so it is what answers "did my push actually land?".
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
def lying_push(monkeypatch):
    """A push that lands on the server and reports failure anyway."""

    def arm(times: int = 1):
        real_push = approval._push
        calls: list[int] = []

        def push_then_lie(branch: str) -> None:
            real_push(branch)
            calls.append(1)
            if len(calls) <= times:
                raise approval.gitrepo.BrainRepoError("error: RPC failed; curl 56 recv failure")

        monkeypatch.setattr(approval, "_push", push_then_lie)

    return arm


class TestLandedCommitFindsTheTrailer:
    def test_it_finds_the_commit_this_feed_created(self, brain):
        feed = _feed()
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert approval.landed_commit(feed.pk, "main") == feed.commit_hash

    def test_it_returns_empty_for_a_feed_never_committed(self, brain):
        assert approval.landed_commit(4242, "main") == ""

    def test_it_does_not_confuse_a_prefix_match(self, brain):
        """`--grep=Feed-Id: 1` also matches `Feed-Id: 12`; the exact
        per-line check is what decides."""
        feed = _feed()
        approval.apply_feed(feed.pk)
        feed.refresh_from_db()

        assert approval.landed_commit(feed.pk, "main") == feed.commit_hash
        assert approval.landed_commit(int(f"{feed.pk}9"), "main") == ""

    def test_it_survives_an_unknown_branch(self, brain):
        assert approval.landed_commit(1, "no-such-branch") == ""


class TestAPushThatLandedButReportedFailure:
    def test_the_feed_is_approved_with_the_real_commit(self, brain, lying_push):
        """The finding. Unfixed this ends as `failed` with no commit
        hash, while the note is live on the origin."""
        lying_push()
        feed = _feed()

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert feed.commit_hash == brain.origin_head()
        assert feed.error == ""

    def test_the_content_is_in_the_brain_exactly_once(self, brain, lying_push):
        lying_push()
        approval.apply_feed(_feed().pk)

        subjects = brain.log_subjects()
        assert subjects.count("feed: src-1") == 1
        assert "> VERBATIM:" in brain.upstream_text(NOTE_PATH)

    def test_it_is_recorded_as_already_landed(self, brain, lying_push):
        from apps.events.models import Event

        lying_push()
        approval.apply_feed(_feed().pk)

        ev = next(
            (e for e in Event.objects.all() if e.details.get("action") == "approved"), None
        )
        assert ev is not None
        assert ev.details["outcome"] == "already-landed"

    def test_it_does_not_burn_every_attempt(self, brain, lying_push):
        lying_push()
        feed = _feed()

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.retries == 1


class TestContentCommittedByHand:
    def test_an_identical_tree_is_approved_not_failed(self, brain):
        """Nobody's commit carries our trailer, but the brain already
        holds exactly what the proposal asks for."""
        proposal = {"files": [raw_file("raw/capture.md", "captured text\n")]}
        brain.push_upstream("raw/capture.md", "captured text\n", "committed by hand")
        feed = _feed(proposal)

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert feed.commit_hash == brain.origin_head()

    def test_it_is_recorded_as_already_present(self, brain):
        from apps.events.models import Event

        brain.push_upstream("raw/capture.md", "captured text\n", "committed by hand")
        approval.apply_feed(_feed({"files": [raw_file()]}).pk)

        ev = next(
            (e for e in Event.objects.all() if e.details.get("action") == "approved"), None
        )
        assert ev.details["outcome"] == "already-present"

    def test_no_empty_commit_is_created(self, brain):
        brain.push_upstream("raw/capture.md", "captured text\n", "committed by hand")
        before = brain.origin_head()

        approval.apply_feed(_feed({"files": [raw_file()]}).pk)

        assert brain.origin_head() == before


class TestTheOrdinaryPathIsUnchanged:
    def test_a_first_time_approval_is_recorded_as_committed(self, brain):
        from apps.events.models import Event

        feed = _feed()
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert feed.retries == 0
        ev = next((e for e in Event.objects.all() if e.details.get("action") == "approved"), None)
        assert ev.details["outcome"] == "committed"

    def test_a_genuinely_failed_push_still_fails(self, brain, monkeypatch):
        """The trailer check must not turn every push error into success."""
        monkeypatch.setattr(
            approval,
            "_push",
            lambda branch: (_ for _ in ()).throw(approval.gitrepo.BrainRepoError("no route to host")),
        )
        feed = _feed()

        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "failed"
        assert feed.commit_hash == ""
        assert "no route to host" in feed.error
