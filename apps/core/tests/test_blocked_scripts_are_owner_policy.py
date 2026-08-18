"""Rule 6 is the owner's policy, declared in their brain, not ours.

The validator hardcoded a rejection of any proposal containing Arabic
script — "the corpus never enters the mind". That is a fact about one
owner's corpus, and it shipped as engine code with no setting, no toggle
and no mention anywhere the operator would look. A stranger installing
this server and feeding it their own notes would have had them rejected
by a rule they could not see or change. The template's contract text was
generalised away from it months ago; the server never followed.

So rule 6 now blocks nothing by default and reads its list from the
brain's own CLAUDE.md, the same file and the same parsing shape the
topic taxonomy already uses.

A misspelled script name would silently disarm the rule, which is the
exact failure mode this change exists to remove — so unknown names come
back as a warning on the result rather than being dropped.
"""
from __future__ import annotations

import pytest

from apps.feeds.services import validator
from apps.feeds.services.validator import (
    BLOCKABLE_SCRIPTS,
    ValidationContext,
    blocked_scripts_from_claude_md,
    validate,
)

ARABIC = "هذه ملاحظة"
HEBREW = "זו הערה"

CLAUDE_MD = """# Brain

## Topic taxonomy

Tags: `ai, writing`

## Blocked scripts

Scripts: `{scripts}`

## Something else
"""


def _proposal(content: str, path: str = "raw/capture.md") -> dict:
    return {"files": [{"path": path, "action": "create", "content": content}]}


def _rule6(result) -> list:
    return [v for v in result.violations if v.rule == 6]


class TestNothingIsBlockedByDefault:
    def test_a_bare_context_blocks_no_script(self) -> None:
        """The finding. A stranger's install must accept their own notes."""
        result = validate(_proposal(ARABIC), ValidationContext())

        assert _rule6(result) == [], (
            "the server rejected a proposal on a rule the owner never asked "
            "for and cannot see"
        )

    def test_a_claude_md_with_no_such_section_blocks_nothing(self) -> None:
        known, unknown = blocked_scripts_from_claude_md("# Brain\n\n## Topic taxonomy\n\nTags: `ai`\n")

        assert (known, unknown) == ((), ())

    def test_an_empty_list_blocks_nothing(self) -> None:
        known, _ = blocked_scripts_from_claude_md(CLAUDE_MD.format(scripts=""))

        assert known == ()


class TestTheSectionCanDocumentItself:
    """The template's own section has to name every script the server
    knows, and shows a commented-out example. Harvesting backticks from
    the whole section — the first version of this parser — turned that
    page of documentation into a blocklist of everything and left the
    example switched on."""

    def test_prose_backticks_are_not_a_blocklist(self) -> None:
        section = """## Blocked scripts

The server recognises: `arabic`, `hebrew`, `cyrillic`, `greek`.
Leave this out and nothing is blocked.

## Next
"""
        known, unknown = blocked_scripts_from_claude_md(section)

        assert (known, unknown) == ((), ()), (
            "documenting the options switched all of them on"
        )

    def test_a_commented_out_example_stays_off(self) -> None:
        section = "## Blocked scripts\n\n<!-- Scripts: `arabic` -->\n"

        assert blocked_scripts_from_claude_md(section) == ((), ())

    def test_the_shipped_template_blocks_nothing(self) -> None:
        """The end-to-end version of both: parse the real file."""
        from pathlib import Path

        from django.conf import settings as dj

        text = (Path(dj.BASE_DIR) / "brain-template/CLAUDE.md").read_text(encoding="utf-8")
        known, unknown = blocked_scripts_from_claude_md(text)

        assert (known, unknown) == ((), ()), (
            "a fresh brain from the template starts with a blocklist"
        )

    def test_the_template_documents_the_section(self) -> None:
        """Config nobody can find is not config."""
        from pathlib import Path

        from django.conf import settings as dj

        text = (Path(dj.BASE_DIR) / "brain-template/CLAUDE.md").read_text(encoding="utf-8")

        assert "Blocked scripts" in text
        for name in BLOCKABLE_SCRIPTS:
            assert name in text, f"{name} is offered but never documented"


class TestABrainCanOptIn:
    def test_the_declared_script_is_rejected(self) -> None:
        known, _ = blocked_scripts_from_claude_md(CLAUDE_MD.format(scripts="arabic"))
        result = validate(_proposal(ARABIC), ValidationContext(blocked_scripts=known))

        assert len(_rule6(result)) == 1
        assert "arabic" in _rule6(result)[0].message
        assert "CLAUDE.md" in _rule6(result)[0].message, (
            "the message has to point at the file the owner can edit"
        )

    def test_an_undeclared_script_passes(self) -> None:
        known, _ = blocked_scripts_from_claude_md(CLAUDE_MD.format(scripts="arabic"))

        assert _rule6(validate(_proposal(HEBREW), ValidationContext(blocked_scripts=known))) == []

    def test_several_scripts_can_be_declared(self) -> None:
        known, _ = blocked_scripts_from_claude_md(CLAUDE_MD.format(scripts="arabic, hebrew"))
        ctx = ValidationContext(blocked_scripts=known)

        assert known == ("arabic", "hebrew")
        assert len(_rule6(validate(_proposal(ARABIC), ctx))) == 1
        assert len(_rule6(validate(_proposal(HEBREW), ctx))) == 1

    def test_latin_text_is_never_caught(self) -> None:
        ctx = ValidationContext(blocked_scripts=tuple(BLOCKABLE_SCRIPTS))

        assert _rule6(validate(_proposal("An ordinary English note."), ctx)) == []

    def test_the_arabic_ranges_are_the_ones_that_shipped(self) -> None:
        """Behaviour for the owner who wants this rule must not change —
        including the presentation forms the old regex covered."""
        ctx = ValidationContext(blocked_scripts=("arabic",))

        for sample in ("؀", "ۿ", "ݐ", "ࢠ", "ﭐ", "﷿", "ﹰ"):
            assert len(_rule6(validate(_proposal(f"text {sample} text"), ctx))) == 1, sample


class TestATypoDoesNotSilentlyDisarmTheRule:
    def test_an_unknown_name_comes_back_separately(self) -> None:
        known, unknown = blocked_scripts_from_claude_md(
            CLAUDE_MD.format(scripts="arabic, arabik")
        )

        assert known == ("arabic",)
        assert unknown == ("arabik",)

    def test_the_operator_is_warned_on_the_result(self) -> None:
        result = validate(_proposal("plain"), ValidationContext(unknown_scripts=("arabik",)))

        warning = " ".join(result.warnings)
        assert "arabik" in warning and "NOT blocked" in warning
        assert "arabic" in warning, "the warning should name the valid options"

    def test_an_unknown_name_alone_is_not_a_violation(self) -> None:
        """A misconfiguration is a warning, not a rejection — it must not
        block the feed the operator is trying to approve."""
        result = validate(_proposal("plain"), ValidationContext(unknown_scripts=("nonsense",)))

        assert result.valid


class TestTheEngineCarriesNoPersonalDefault:
    def test_no_module_level_arabic_regex_survives(self) -> None:
        assert not hasattr(validator, "_ARABIC_RE"), (
            "a hardcoded personal rule came back at module scope"
        )

    def test_the_context_default_is_empty(self) -> None:
        assert ValidationContext().blocked_scripts == ()

    @pytest.mark.parametrize("name", sorted(BLOCKABLE_SCRIPTS))
    def test_every_offered_script_compiles_and_matches_itself(self, name) -> None:
        """The table is the vocabulary the warning advertises, so every
        entry has to be usable."""
        ctx = ValidationContext(blocked_scripts=(name,))
        sample = {
            "arabic": "س", "hebrew": "ש", "cyrillic": "д", "greek": "δ",
            "devanagari": "क", "thai": "ก", "han": "字",
            "hiragana": "ひ", "katakana": "カ", "hangul": "한",
        }[name]

        assert len(_rule6(validate(_proposal(sample), ctx))) == 1
