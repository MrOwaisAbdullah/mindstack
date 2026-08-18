"""A nested paren inside an `(agents-only: ...)` span leaked it to public.

The public tier is built by stripping inline agents-only spans out of
otherwise-public notes (CLAUDE.md §4). The stripper was:

    re.compile(r"\\(agents-only:[^)]*\\)", re.DOTALL)

`[^)]*` stops at the FIRST closing paren, so any span containing a nested
one — a parenthetical aside, a citation, a URL like `x.com/a_(b)` — was
matched only up to that inner paren. The remainder of the secret text was
written verbatim into the public snapshot.

That is the visibility model failing at its only load-bearing claim: not
"the public caller is shown less" but "the bytes are not in the directory
it reads". A regex that cannot count parens cannot make that promise.

The unbalanced case is the other half. `(agents-only: secret` with no
closing paren never matched at all, so the whole thing went public — the
worst possible reading of a typo. It now fails closed: everything from
the marker to end of file is dropped and the file is named in a warning.
A note that is mysteriously short in public is recoverable; one that
published a secret is not.
"""
from __future__ import annotations

import logging

import pytest

from apps.brain.services import snapshots
from apps.brain.services.snapshots import strip_agents_only_spans as strip

SECRET = "SHOULD-NOT-BE-PUBLIC"


class TestNestedParensNoLongerLeak:
    """Each of these leaked the tail of the span before the fix."""

    def test_a_parenthetical_aside_inside_a_span(self) -> None:
        out = strip(f"Before. (agents-only: hint (an aside) {SECRET}) After.")

        assert SECRET not in out
        assert out == "Before.  After."

    def test_a_url_containing_parens(self) -> None:
        out = strip(f"Before. (agents-only: see https://x.com/a_(b) {SECRET}) After.")

        assert SECRET not in out
        assert out == "Before.  After."

    def test_several_levels_of_nesting(self) -> None:
        out = strip(f"A (agents-only: x (y (z) y) {SECRET}) B")

        assert SECRET not in out
        assert out == "A  B"

    def test_the_stray_close_paren_goes_too(self) -> None:
        """The old bug left a dangling `)` even when it stripped most of
        the span — visible punctuation debris in published notes."""
        out = strip("A (agents-only: x (y) z) B")

        assert ")" not in out


class TestAnUnbalancedSpanFailsClosed:
    def test_everything_after_the_marker_is_dropped(self) -> None:
        out = strip(f"Public part. (agents-only: {SECRET} and it never closes")

        assert SECRET not in out
        assert out == "Public part. "

    def test_an_unclosed_nested_paren_also_fails_closed(self) -> None:
        out = strip(f"Public part. (agents-only: oops ( {SECRET})")

        assert SECRET not in out
        assert out == "Public part. "

    def test_content_before_the_marker_survives(self) -> None:
        """Fail closed, not fail empty: the note is truncated at the
        marker, not discarded."""
        out = strip("Keep this paragraph.\n\n(agents-only: unterminated")

        assert out == "Keep this paragraph.\n\n"

    def test_the_file_is_named_in_a_warning(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=snapshots.log.name):
            strip("x (agents-only: unterminated", source="knowledge/takes/a.md")

        assert "knowledge/takes/a.md" in caplog.text

    def test_a_balanced_span_logs_nothing(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=snapshots.log.name):
            strip("x (agents-only: fine) y", source="knowledge/takes/a.md")

        assert caplog.text == ""


class TestTheBehaviourThatAlreadyWorked:
    """Regression guards — these passed before the fix and must keep
    passing. The bug was in what the regex could not count, not in what
    it did with ordinary spans."""

    def test_a_plain_span(self) -> None:
        assert strip(f"A (agents-only: {SECRET}) B") == "A  B"

    def test_two_spans_on_one_line(self) -> None:
        out = strip(f"A (agents-only: {SECRET}) B (agents-only: {SECRET}2) C")

        assert SECRET not in out
        assert out == "A  B  C"

    def test_a_span_across_lines(self) -> None:
        out = strip(f"A (agents-only: line one\nline two {SECRET}) B")

        assert SECRET not in out
        assert out == "A  B"

    def test_ordinary_parentheses_are_untouched(self) -> None:
        text = "A normal note (with an aside) and a url https://x.com/a_(b) here."

        assert strip(text) == text

    def test_text_with_no_span_is_returned_unchanged(self) -> None:
        assert strip("# Title\n\nBody.\n") == "# Title\n\nBody.\n"

    def test_a_bare_marker_word_is_not_a_span(self) -> None:
        """`agents-only` as prose must not trigger anything."""
        text = "This note is agents-only in spirit but not marked.\n"

        assert strip(text) == text


@pytest.mark.django_db
class TestTheSecretNeverReachesThePublicSnapshot:
    """The cross-mechanism check. Unit-testing the stripper proves the
    function; this proves the tier, which is the thing actually promised.
    """

    @pytest.fixture()
    def views(self, tmp_path, settings, monkeypatch):
        from apps.brain.models import Entity

        repo = tmp_path / "repo"
        (repo / "knowledge" / "takes").mkdir(parents=True)
        (repo / "knowledge" / "takes" / "note.md").write_text(
            "---\nid: note\nvisibility: public\n---\n\n"
            f"# note\n\nPublic sentence. (agents-only: aside (nested) {SECRET}) End.\n",
            encoding="utf-8",
        )
        Entity.objects.create(
            entity_id="note", kind="take", path="knowledge/takes/note.md",
            title="note", visibility="public",
        )
        settings.BRAIN_REPO_DIR = repo
        settings.BRAIN_VIEWS_DIR = tmp_path / "views"
        monkeypatch.setattr(snapshots.gitrepo, "head_sha", lambda: "a" * 40)
        return tmp_path / "views"

    def test_no_file_in_the_public_tier_contains_it(self, views) -> None:
        snapshots.build_all()

        offenders = [
            p.relative_to(views).as_posix()
            for p in (views / "public").rglob("*")
            if p.is_file() and SECRET in p.read_text(encoding="utf-8", errors="replace")
        ]

        assert offenders == [], f"agents-only content reached the public tier: {offenders}"

    def test_the_agents_tier_still_has_it(self, views) -> None:
        """The span is stripped for public only. If it vanished from
        agents-only too, the fix would be destroying content instead of
        containing it."""
        snapshots.build_all()

        text = (views / "agents-only" / "knowledge/takes/note.md").read_text(encoding="utf-8")

        assert SECRET in text
