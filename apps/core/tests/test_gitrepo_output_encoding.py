"""`_git` must decode git's output as UTF-8, not the host locale.

`subprocess.run(..., text=True)` without `encoding=` decodes with
`locale.getpreferredencoding(False)`. In the Linux containers that is
UTF-8 and nothing ever looked wrong. On a Windows host running the app
directly it is cp1252, so every em dash in a commit subject — this
project's own commit style — came back as `â€"`, and any accented
author or path was similarly mangled in whatever surface echoes git
output (sync errors, ops flashes, commit subjects).

This test only bites on a host whose preferred encoding is not UTF-8 —
which is exactly the population the bug ships to. On a UTF-8 host it
passes with or without the fix; it was proven to fail pre-fix on the
Windows host this repo is developed on (cp1252).
"""
from __future__ import annotations

import subprocess

import pytest

from apps.brain.services import gitrepo

SUBJECT = "feat(reader): café — the em dash commit"


@pytest.fixture()
def repo_with_utf8_subject(tmp_path, settings, monkeypatch):
    def run(*a, cwd):
        proc = subprocess.run(
            ["git", *a], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
        )
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(a)}: {proc.stderr or proc.stdout}")
        return proc.stdout.strip()

    clone = tmp_path / "brain-repo"
    clone.mkdir()
    run("init", "-b", "main", cwd=clone)
    run("config", "user.name", "T", cwd=clone)
    run("config", "user.email", "t@localhost", cwd=clone)
    run("config", "commit.gpgsign", "false", cwd=clone)
    (clone / "note.md").write_text("hello\n", encoding="utf-8", newline="\n")
    run("add", "-A", cwd=clone)
    run("commit", "-m", SUBJECT, cwd=clone)

    settings.BRAIN_REPO_DIR = clone
    monkeypatch.setattr(gitrepo, "_git_env", lambda: None)
    return clone


def test_git_output_survives_a_non_utf8_locale(repo_with_utf8_subject) -> None:
    out = gitrepo._git("log", "-1", "--format=%s", cwd=repo_with_utf8_subject)
    assert out.strip() == SUBJECT
