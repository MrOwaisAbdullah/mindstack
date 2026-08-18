"""An INDEX line may only describe a file the proposal carries.

Rule 7 asked one direction: does every proposed *file* have an index
line. Nothing asked the reverse, and the reverse is where the damage is.

`diffview.build` renders one diff per proposed FILE and never shows
INDEX.md, so an index line naming an unrelated entity is invisible to
the operator approving the feed. `_apply_index_lines` then overwrites
the first existing INDEX.md line whose text ends with the extracted
path — so the commit that lands is not the commit that was reviewed.

The degenerate case is worse: `path` is read as the last `|`-separated
segment, so a line ending in `|` yields "" — and `endswith("")` is true
for every line, silently clobbering whichever entity is listed first.

`feeder.normalize_index_lines` already declines to recompose an entry
with no proposed file, with the comment "the validator judges it". It
did not.
"""
from __future__ import annotations

import pytest

from apps.feeds.models import Feed
from apps.feeds.services import approval, validator

from .conftest import index_line, note

pytestmark = pytest.mark.django_db


SEEDED_INDEX = """# INDEX

## Knowledge
- [take] Existing note — the real description | status: current | knowledge/takes/existing.md
- [fact] Another — second line | status: current | knowledge/facts/another.md

## Projects

## Content
"""


@pytest.fixture()
def seeded(brain):
    """A clone whose INDEX.md already describes two unrelated entities."""
    (brain.clone / "INDEX.md").write_text(SEEDED_INDEX, encoding="utf-8", newline="\n")
    brain.git("add", "-A")
    brain.git("commit", "-m", "seed index")
    brain.git("push", "origin", "main")
    return brain


def _feed(proposal: dict) -> Feed:
    return Feed.objects.create(
        source_id="src-1",
        channel="ui",
        status="approving",
        raw_payload={"source_kind": "blog"},
        proposal=proposal,
    )


def _ctx():
    ctx = validator.context_from_repo()
    ctx.source_kind = "blog"
    return ctx


class TestIndexLinePath:
    """Validator and apply code must read a line the same way."""

    def test_reads_the_last_pipe_segment(self):
        assert validator.index_line_path("- [take] X — d | status: current | k/t/x.md") == "k/t/x.md"

    def test_a_trailing_pipe_yields_nothing(self):
        assert validator.index_line_path("- [take] X — d |") == ""

    def test_a_line_with_no_pipe_yields_the_whole_line(self):
        assert validator.index_line_path("- [take] X — d k/t/x.md") == "- [take] X — d k/t/x.md"


class TestTheValidatorRejectsOrphanLines:
    def test_a_line_for_an_entity_with_no_file(self, brain):
        proposal = {
            "files": [note("a-take")],
            "index_lines": [
                index_line("a-take"),
                {
                    "entity_id": "existing",
                    "line": "- [take] Existing — REWRITTEN | status: current | knowledge/takes/existing.md",
                },
            ],
        }
        res = validator.validate(proposal, _ctx())

        assert not res.valid
        assert any("names no file in this proposal" in v.message for v in res.violations)

    def test_the_empty_path_clobber(self, brain):
        proposal = {
            "files": [note("a-take")],
            "index_lines": [{"entity_id": "a-take", "line": "- [take] A take — hook |"}],
        }
        res = validator.validate(proposal, _ctx())

        assert not res.valid
        assert any("must end with" in v.message for v in res.violations)

    def test_a_line_pointing_at_a_different_path(self, brain):
        proposal = {
            "files": [note("a-take")],
            "index_lines": [
                {
                    "entity_id": "a-take",
                    "line": "- [take] A take — hook | status: current | knowledge/takes/existing.md",
                }
            ],
        }
        res = validator.validate(proposal, _ctx())

        assert not res.valid
        assert any("knowledge/takes/existing.md" in v.message for v in res.violations)

    def test_a_well_formed_proposal_still_passes(self, brain):
        proposal = {"files": [note("a-take")], "index_lines": [index_line("a-take")]}
        res = validator.validate(proposal, _ctx())

        assert res.valid, [str(v) for v in res.violations]


class TestNothingUnrelatedIsRewritten:
    def test_the_orphan_line_never_reaches_index_md(self, seeded):
        feed = _feed(
            {
                "files": [note("a-take")],
                "index_lines": [
                    index_line("a-take"),
                    {
                        "entity_id": "existing",
                        "line": "- [take] Existing — REWRITTEN | status: current | knowledge/takes/existing.md",
                    },
                ],
            }
        )
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "failed"
        assert "the real description" in (seeded.clone / "INDEX.md").read_text(encoding="utf-8")
        assert "REWRITTEN" not in seeded.upstream_text("INDEX.md")

    def test_the_empty_path_does_not_clobber_the_first_entry(self, seeded):
        """The reachable shape of the empty-path bug.

        The proposal carries a correct line for its own note, so forward
        rule 7 is satisfied and never looks at the second entry. That
        entry's `entity_id` matches no proposed file, so nothing looked at
        it at all — and its empty path made `endswith("")` match the very
        first `- ` line in INDEX.md. Unfixed, this feed is APPROVED and
        PUSHED with the existing entity's line destroyed.
        """
        feed = _feed(
            {
                "files": [note("a-take")],
                "index_lines": [
                    index_line("a-take"),
                    {"entity_id": "orphan", "line": "- [take] Orphan — hook |"},
                ],
            }
        )
        approval.apply_feed(feed.pk)

        committed = seeded.upstream_text("INDEX.md")
        assert "- [take] Existing note — the real description" in committed
        assert "Orphan" not in committed
        feed.refresh_from_db()
        assert feed.status == "failed"


class TestTheApplyGuardHoldsWithoutTheValidator:
    """The validator is the gate; `_apply_index_lines` is the lock."""

    def test_orphan_line_raises_at_apply_time(self, seeded):
        proposal = {
            "files": [note("a-take")],
            "index_lines": [
                {"entity_id": "x", "line": "- [take] X — y | knowledge/takes/existing.md"}
            ],
        }
        with pytest.raises(approval.ApplyFailure, match="not a file this proposal carries"):
            approval._apply_index_lines(seeded.clone, proposal)

    def test_empty_path_raises_at_apply_time(self, seeded):
        proposal = {
            "files": [note("a-take")],
            "index_lines": [{"entity_id": "a-take", "line": "- [take] A take — hook |"}],
        }
        with pytest.raises(approval.ApplyFailure, match="not a file this proposal carries"):
            approval._apply_index_lines(seeded.clone, proposal)

    def test_index_md_is_untouched_when_it_raises(self, seeded):
        before = (seeded.clone / "INDEX.md").read_text(encoding="utf-8")
        proposal = {
            "files": [note("a-take")],
            "index_lines": [
                index_line("a-take"),
                {"entity_id": "x", "line": "- [take] X — y |"},
            ],
        }
        with pytest.raises(approval.ApplyFailure):
            approval._apply_index_lines(seeded.clone, proposal)
        assert (seeded.clone / "INDEX.md").read_text(encoding="utf-8") == before

    def test_a_legitimate_line_still_applies(self, seeded):
        proposal = {"files": [note("a-take")], "index_lines": [index_line("a-take")]}
        approval._apply_index_lines(seeded.clone, proposal)

        index = (seeded.clone / "INDEX.md").read_text(encoding="utf-8")
        assert "knowledge/takes/a-take.md" in index
        assert "- [take] Existing note — the real description" in index


class TestTheWholePipelineStillLands:
    def test_a_valid_feed_commits_its_index_line(self, seeded):
        feed = _feed({"files": [note("a-take")], "index_lines": [index_line("a-take")]})
        approval.apply_feed(feed.pk)

        feed.refresh_from_db()
        assert feed.status == "approved", feed.error
        committed = seeded.upstream_text("INDEX.md")
        assert "knowledge/takes/a-take.md" in committed
        assert "the real description" in committed
