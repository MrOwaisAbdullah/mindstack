"""The gate has to run before the door opens.

`apply_feed` wrote the whole proposal into the shared clone and *then*
called `validator.validate`. Every invalid proposal — every one, not an
edge case — landed on disk before anything checked whether it was
allowed to, and only `_rollback` took it back out again. That makes a
correctness property depend on cleanup succeeding, in a clone three
containers share and that `indexer.rebuild()` reads as the working tree.

The reorder is semantics-preserving: `validate` is pure over
(proposal, ctx), and `ctx` was already built from the pre-apply repo, so
it computes the same verdict in either position. These tests pin the
order structurally, because after the rollback fix both orders end with
a clean tree — the difference is whether the bytes were ever there.
"""
from __future__ import annotations

import pytest

from apps.feeds.models import Feed
from apps.feeds.services import approval, validator

from .conftest import index_line, note, raw_file

pytestmark = pytest.mark.django_db


def _feed(proposal: dict) -> Feed:
    return Feed.objects.create(
        source_id="src-1",
        channel="ui",
        status="approving",
        raw_payload={"source_kind": "blog"},
        proposal=proposal,
    )


@pytest.fixture()
def dirt_at_rollback(monkeypatch):
    """`git status --porcelain` as it stood the moment rollback ran.

    Asserting on the tree AFTER `apply_feed` proves nothing about this
    finding — the rollback fix leaves it clean either way. The question
    here is whether the bytes were ever written at all, and the last
    moment they would still be visible is just before the reset.
    """
    seen: list[str] = []
    real = approval._rollback

    def spy(branch):
        seen.append(approval.gitrepo.run("status", "--porcelain"))
        return real(branch)

    monkeypatch.setattr(approval, "_rollback", spy)
    return seen


@pytest.fixture()
def spy(monkeypatch):
    """Record whether each apply step ran, without changing behaviour."""
    calls: list[str] = []
    for name in ("_apply_files", "_apply_index_lines", "_apply_supersedes", "_apply_taxonomy"):
        real = getattr(approval, name)

        def wrapper(*a, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*a, **kw)

        monkeypatch.setattr(approval, name, wrapper)
    return calls


#: Fails rule 3 (topic outside the CLAUDE.md taxonomy) — an ordinary
#: content mistake, not a path trick, so nothing else can reject it first.
INVALID = {"files": [note("a-take", topics="not-a-real-tag")], "index_lines": [index_line("a-take")]}
VALID = {"files": [note("a-take")], "index_lines": [index_line("a-take")]}


class TestNothingIsWrittenForAnInvalidProposal:
    def test_no_apply_step_runs(self, brain, spy):
        approval.apply_feed(_feed(INVALID).pk)
        assert spy == []

    def test_the_clone_is_never_dirtied(self, brain, dirt_at_rollback):
        """Unfixed, this shows the untracked note and a modified INDEX.md."""
        approval.apply_feed(_feed(INVALID).pk)
        assert dirt_at_rollback == [""]

    def test_the_note_is_not_in_the_clone_afterwards(self, brain):
        approval.apply_feed(_feed(INVALID).pk)
        assert not (brain.clone / "knowledge" / "takes" / "a-take.md").exists()
        assert brain.dirty() == ""

    def test_the_failure_reports_the_violation_not_a_crash(self, brain):
        feed = _feed(INVALID)
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "failed"
        assert "pre-commit validation failed" in feed.error
        assert "not-a-real-tag" in feed.error


class TestAValidProposalStillApplies:
    def test_every_step_runs_in_order(self, brain, spy):
        approval.apply_feed(_feed(VALID).pk)
        assert spy == ["_apply_files", "_apply_index_lines", "_apply_supersedes", "_apply_taxonomy"]

    def test_it_commits_and_pushes(self, brain):
        feed = _feed(VALID)
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        assert brain.origin_head() == feed.commit_hash
        assert "id: a-take" in brain.upstream_text("knowledge/takes/a-take.md")

    def test_the_index_line_lands_under_its_section(self, brain):
        approval.apply_feed(_feed(VALID).pk)

        lines = brain.upstream_text("INDEX.md").splitlines()
        assert "## Knowledge" in lines
        at = lines.index("## Knowledge")
        assert lines[at + 1].endswith("knowledge/takes/a-take.md")


class TestTheVerdictIsUnchangedByTheReorder:
    """`validate` must not depend on the apply having happened — the whole
    licence for moving it."""

    def test_same_result_before_and_after_applying(self, brain):
        ctx = validator.context_from_repo()
        ctx.source_kind = "blog"
        before = validator.validate(VALID, ctx)

        approval._apply_files(brain.clone, VALID)
        approval._apply_index_lines(brain.clone, VALID)
        after = validator.validate(VALID, ctx)

        assert before.valid is after.valid is True
        assert [str(v) for v in before.violations] == [str(v) for v in after.violations]

    def test_same_result_for_an_invalid_proposal_too(self, brain):
        ctx = validator.context_from_repo()
        ctx.source_kind = "blog"
        before = validator.validate(INVALID, ctx)

        approval._apply_files(brain.clone, INVALID)
        after = validator.validate(INVALID, ctx)

        assert before.valid is after.valid is False
        assert [str(v) for v in before.violations] == [str(v) for v in after.violations]


class TestValidationFailureIsStillTerminal:
    def test_it_does_not_burn_the_push_retries(self, brain):
        """An invalid proposal is not a race; replaying it three times
        would just write it three times."""
        feed = _feed(INVALID)
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.retries == 0

    def test_the_origin_does_not_move(self, brain):
        before = brain.origin_head()
        approval.apply_feed(_feed(INVALID).pk)
        assert brain.origin_head() == before


class TestRawOnlyProposalsAreUnaffected:
    def test_a_raw_capture_still_applies(self, brain):
        feed = _feed({"files": [raw_file()]})
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
