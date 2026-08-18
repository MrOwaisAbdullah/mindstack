"""Every `docs/SECURITY.md` pointer leads somewhere.

Five places told the operator to read a file that did not exist: two
lines in `.env.example`, the write-credential step of the setup wizard,
the help text on the `BRAIN_GIT_WRITE_PAT` setting, and a module
docstring in `gitcreds`. Each of them at the exact moment the operator
was making the decision the document was supposed to inform.

This checks the pointers resolve, and that the document still covers the
questions those five pointers send someone to it with — the write PAT's
database-versus-file tradeoff, the ops perimeter, and how to report a
vulnerability. Prose is not asserted beyond that; a doc test that pins
sentences gets deleted the first time someone edits the doc.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs/SECURITY.md"

#: Where the file is pointed at from. Kept explicit rather than
#: discovered, so deleting a reference is a deliberate edit here too.
_REFERRERS = (
    ".env.example",
    "templates/setup/write.html",
    "apps/brainconfig/services.py",
    "apps/brain/services/gitcreds.py",
)


def test_the_document_exists() -> None:
    assert DOC.is_file(), (
        "five places in the product tell the operator to read this file"
    )


@pytest.mark.parametrize("referrer", _REFERRERS)
def test_each_referrer_still_points_at_it(referrer: str) -> None:
    """The other direction: if a pointer is removed, this test should be
    updated deliberately rather than passing by accident."""
    text = (REPO_ROOT / referrer).read_text(encoding="utf-8")

    assert "SECURITY.md" in text, f"{referrer} no longer references the document"


def test_no_reference_points_at_a_missing_doc() -> None:
    """The general form. Any `docs/NAME.md` mentioned in shipped code or
    templates has to exist — this is the class of bug, not just the one
    instance."""
    corpus = [
        p
        for root in ("apps", "config", "templates")
        for p in (REPO_ROOT / root).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".html"}
        and "__pycache__" not in p.parts
        and "tests" not in p.parts
    ] + [REPO_ROOT / ".env.example"]

    dangling = []
    for path in corpus:
        for name in re.findall(r"docs/([A-Za-z0-9_.-]+\.md)", path.read_text(encoding="utf-8")):
            if not (REPO_ROOT / "docs" / name).is_file():
                dangling.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> docs/{name}")

    assert not dangling, f"these point at docs that do not exist: {dangling}"


def test_it_answers_the_questions_it_is_cited_for() -> None:
    text = DOC.read_text(encoding="utf-8")

    for topic in (
        "BRAIN_GIT_WRITE_PAT_PATH",   # the file-vs-database tradeoff
        "FIELD_ENCRYPTION_KEY",       # what encryption at rest does and does not do
        "ADMIN_IP_ALLOWLIST",         # the ops perimeter
        "Reporting a vulnerability",  # the thing a SECURITY.md is for
    ):
        assert topic in text, f"SECURITY.md never mentions {topic}"


def test_it_states_the_limits_and_not_only_the_wins() -> None:
    """The checklist item asks for "the §9 honest truths". A security
    document listing only mitigations is marketing."""
    text = DOC.read_text(encoding="utf-8")

    assert "What this does not protect" in text
    assert "as private as the machine" in text
