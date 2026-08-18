"""Every management command must be runnable in this repo, not the one
it was vendored from.

Three upstream codegen commands — `dump_audit_actions`, `dump_events`,
`dump_csp_directives` — each rewrote a maintainer doc between sentinel
comments: docs/RATE_LIMIT_AND_AUDIT.md, docs/EVENTS.md, docs/SECURITY.md.
None of those docs was ever part of this repo, so all three opened with
`if not DOC.exists(): raise CommandError` and had raised on every
invocation for the life of the project. They are deleted; these checks
keep the class of bug from riding back in on the next vendored file.

Structural on purpose: a command that always errors passes every
behavioural test nobody thinks to write for it.
"""
from __future__ import annotations

import re
from importlib import import_module
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The `_X_DOC = _REPO_ROOT / "docs" / "NAME.md"` codegen-target shape the
# vendored commands shared. Prose mentions of docs don't match this.
_DOC_JOIN_RE = re.compile(r'/\s*"docs"\s*/\s*"([^"]+)"')


def _command_files() -> list[Path]:
    files = [
        p
        for p in _REPO_ROOT.glob("apps/*/management/commands/*.py")
        if p.name != "__init__.py"
    ]
    assert files, "no management commands found — the glob is broken"
    return files


def test_every_command_module_imports() -> None:
    """`ModuleNotFoundError` at import time is how the sync_scheduled →
    config.scheduled breakage shipped. Importing costs nothing and
    catches any command whose upstream dependencies were never ported."""
    for path in _command_files():
        dotted = ".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts)
        import_module(dotted)


def test_no_command_targets_a_repo_doc_that_does_not_exist() -> None:
    for path in _command_files():
        source = path.read_text(encoding="utf-8")
        for name in _DOC_JOIN_RE.findall(source):
            target = _REPO_ROOT / "docs" / name
            assert target.exists(), (
                f"{path.relative_to(_REPO_ROOT)} rewrites docs/{name}, which "
                f"does not exist in this repo — the command can only ever "
                f"raise CommandError. Port the doc or delete the command."
            )


@pytest.mark.parametrize(
    "gone", ["dump_audit_actions", "dump_events", "dump_csp_directives"]
)
def test_the_vendored_codegen_commands_stay_gone(gone: str) -> None:
    """Deleted rather than fixed: their target docs describe the upstream
    product (UPDATES.md #11 codegen, `make audit-actions-check`, an
    `apps/audit` app), none of which exists here. If one of these docs
    is ever actually written, resurrect the tool from git history —
    don't let the command return without its doc."""
    assert not (
        _REPO_ROOT / "apps/core/management/commands" / f"{gone}.py"
    ).exists(), f"{gone} is back without its target doc"
