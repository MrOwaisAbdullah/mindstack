"""Shared fixtures for tests that need a real brain clone.

The approval pipeline is git, not an abstraction over git: rollback,
replay after a push race, and "nothing to commit" are behaviours of the
actual command, and the findings they cover are all about what the tree
looks like afterwards. Mocking `gitrepo.run` would test the mock, so
these fixtures build a real bare origin, a real clone, and a second
working copy that stands in for "somebody else pushed".

Everything lives under `tmp_path`, so nothing here can touch the real
`data/brain-repo`.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.brain.services import gitrepo
from apps.feeds.services import approval

#: Enough of the brain layout for `context_from_repo()` and the INDEX
#: rewriter to work. Not the full CONTRACT_PATHS set — `apply_feed` never
#: calls `verify_contract()`.
SEED_CLAUDE_MD = """# Brain

## Topic taxonomy

Tags: `ai, writing, systems`

## Something else
"""

SEED_INDEX_MD = """# INDEX

## Knowledge

## Projects

## Content
"""


def _run(*args: str, cwd: Path) -> str:
    # encoding is explicit: `text=True` alone decodes with the host locale,
    # which on Windows is cp1252 and turns every em dash in an INDEX line
    # into mojibake — an assertion failure that looks like a logic bug.
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}: {proc.stderr or proc.stdout}"
        )
    return proc.stdout.strip()


def _identity(work: Path) -> None:
    _run("config", "user.name", "Test Operator", cwd=work)
    _run("config", "user.email", "test@localhost", cwd=work)
    _run("config", "commit.gpgsign", "false", cwd=work)
    _run("config", "core.autocrlf", "false", cwd=work)


@dataclass
class Brain:
    """A brain clone, its origin, and a second working copy of the origin."""

    clone: Path
    origin: Path
    upstream: Path

    def git(self, *args: str) -> str:
        return _run(*args, cwd=self.clone)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def origin_head(self) -> str:
        return _run("rev-parse", "main", cwd=self.origin)

    def dirty(self) -> str:
        """`git status --porcelain` — "" means the rollback was complete."""
        return self.git("status", "--porcelain")

    def log_subjects(self) -> list[str]:
        return _run("log", "--format=%s", "main", cwd=self.origin).splitlines()

    def push_upstream(self, path: str, content: str, message: str = "upstream edit") -> str:
        """Somebody else commits to the origin — the push-race setup."""
        _run("pull", "--ff-only", cwd=self.upstream)
        target = self.upstream / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        _run("add", "-A", cwd=self.upstream)
        _run("commit", "-m", message, cwd=self.upstream)
        _run("push", "origin", "main", cwd=self.upstream)
        return _run("rev-parse", "HEAD", cwd=self.upstream)

    def upstream_text(self, path: str) -> str:
        """The file's content as it stands on the origin right now."""
        return _run("show", f"main:{path}", cwd=self.origin)


@pytest.fixture()
def brain(tmp_path, settings, monkeypatch) -> Brain:
    origin = tmp_path / "origin.git"
    clone = tmp_path / "brain-repo"
    upstream = tmp_path / "upstream"

    _run("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    clone.mkdir()
    _run("init", "-b", "main", cwd=clone)
    _identity(clone)
    (clone / "CLAUDE.md").write_text(SEED_CLAUDE_MD, encoding="utf-8", newline="\n")
    (clone / "INDEX.md").write_text(SEED_INDEX_MD, encoding="utf-8", newline="\n")
    for d in ("knowledge/takes", "knowledge/facts", "projects", "raw", "identity", "lenses"):
        (clone / d).mkdir(parents=True, exist_ok=True)
        (clone / d / ".gitkeep").write_text("", encoding="utf-8")
    _run("add", "-A", cwd=clone)
    _run("commit", "-m", "seed", cwd=clone)
    _run("remote", "add", "origin", origin.as_posix(), cwd=clone)
    _run("push", "-u", "origin", "main", cwd=clone)

    # `-c core.autocrlf=false` on the CLONE, not just afterwards: this host
    # may have autocrlf=true globally, in which case the checkout writes
    # CRLF and the next `add -A` (with autocrlf since turned off) commits
    # those CRLFs back — rewriting every line of every seeded file and
    # making an unrelated upstream commit look like it touched INDEX.md.
    _run("-c", "core.autocrlf=false", "clone", origin.as_posix(), str(upstream), cwd=tmp_path)
    _identity(upstream)

    settings.BRAIN_REPO_DIR = clone
    settings.BRAIN_VIEWS_DIR = tmp_path / "brain-views"
    # No credentials in a test: push/fetch go to the local path origin, and
    # `_git_env` would otherwise materialise an ssh key from the database.
    monkeypatch.setattr(approval, "_write_pat", lambda: "")
    monkeypatch.setattr(gitrepo, "_git_env", lambda: None)
    return Brain(clone=clone, origin=origin, upstream=upstream)


# ---- proposal builders ---------------------------------------------------


def raw_file(path: str = "raw/capture.md", content: str = "captured text\n") -> dict:
    """The simplest file a proposal can carry that passes rules 1-8:
    `raw/` has no frontmatter schema and needs no INDEX line."""
    return {"path": path, "action": "create", "content": content}


def note(entity_id: str = "a-take", *, visibility: str = "public", topics: str = "ai") -> dict:
    """A knowledge note that satisfies the note rules (1-4, 6)."""
    return {
        "path": f"knowledge/takes/{entity_id}.md",
        "action": "create",
        "content": (
            "---\n"
            f"id: {entity_id}\n"
            "type: take\n"
            f"topics: [{topics}]\n"
            "source: raw/capture.md\n"
            "source_url: https://example.com/post\n"
            "date: 2026-08\n"
            "status: current\n"
            f"visibility: {visibility}\n"
            "---\n"
            "\n# A take\n\n> VERBATIM: the thing that was said.\n"
        ),
    }


def index_line(entity_id: str = "a-take", *, visibility: str = "public") -> dict:
    """The rule 7 companion to `note()`."""
    vis = "" if visibility == "public" else f" | visibility: {visibility}"
    return {
        "entity_id": entity_id,
        "line": f"- [take] A take — a description | status: current{vis} | knowledge/takes/{entity_id}.md",
    }
