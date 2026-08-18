"""A conflicting pull must not wedge the clone forever.

Nothing in this codebase ever ran `rebase --abort`. So a `pull --rebase`
that hit a conflict left `.git/rebase-merge` in place, and from then on:
every later pull failed with "there is already a rebase-merge
directory", `working_tree_dirty()` read True, and `replace_clone` — the
documented repair — refused for exactly that reason. The brain served
whatever it happened to have, permanently, and no ops action could move
it.

Abort rather than `reset --hard`: the pre-rebase state can hold commits
that exist nowhere else — an approved feed that committed here but
failed to push — and destroying those is the thing `replace_clone`
guards against.
"""
from __future__ import annotations

import subprocess

import pytest

from apps.brain.services import gitrepo


def _git(*args: str, cwd) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    return proc.stdout.strip()


@pytest.fixture()
def conflicted(tmp_path, settings, monkeypatch):
    """A clone whose next `pull --rebase` is guaranteed to conflict.

    Origin and the clone each commit a different line to the same file.
    """
    origin = tmp_path / "origin.git"
    clone = tmp_path / "brain-repo"
    other = tmp_path / "other"

    def run(*a, cwd):
        proc = subprocess.run(
            ["git", *a], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
        )
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(a)}: {proc.stderr or proc.stdout}")
        return proc.stdout.strip()

    def identity(work):
        run("config", "user.name", "T", cwd=work)
        run("config", "user.email", "t@localhost", cwd=work)
        run("config", "commit.gpgsign", "false", cwd=work)
        run("config", "core.autocrlf", "false", cwd=work)

    run("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    clone.mkdir()
    run("init", "-b", "main", cwd=clone)
    identity(clone)
    (clone / "note.md").write_text("base\n", encoding="utf-8", newline="\n")
    run("add", "-A", cwd=clone)
    run("commit", "-m", "base", cwd=clone)
    run("remote", "add", "origin", origin.as_posix(), cwd=clone)
    run("push", "-u", "origin", "main", cwd=clone)

    run("-c", "core.autocrlf=false", "clone", origin.as_posix(), str(other), cwd=tmp_path)
    identity(other)
    (other / "note.md").write_text("theirs\n", encoding="utf-8", newline="\n")
    run("add", "-A", cwd=other)
    run("commit", "-m", "upstream edit", cwd=other)
    run("push", "origin", "main", cwd=other)

    # A local commit on the same line — the rebase cannot resolve it.
    (clone / "note.md").write_text("ours\n", encoding="utf-8", newline="\n")
    run("add", "-A", cwd=clone)
    run("commit", "-m", "local edit", cwd=clone)
    local_head = run("rev-parse", "HEAD", cwd=clone)

    settings.BRAIN_REPO_DIR = clone
    monkeypatch.setattr(gitrepo, "_git_env", lambda: None)
    monkeypatch.setattr(gitrepo, "verify_contract", lambda: None)
    return clone, local_head


class TestTheConflictIsCleanedUp:
    def test_the_pull_still_reports_failure(self, conflicted):
        with pytest.raises(gitrepo.BrainRepoError):
            gitrepo.pull_rebase()

    def test_no_rebase_is_left_in_progress(self, conflicted):
        """The finding. Unfixed, `.git/rebase-merge` survives."""
        with pytest.raises(gitrepo.BrainRepoError):
            gitrepo.pull_rebase()

        assert gitrepo.rebase_in_progress() is False

    def test_the_working_tree_is_clean_again(self, conflicted):
        with pytest.raises(gitrepo.BrainRepoError):
            gitrepo.pull_rebase()

        assert gitrepo.working_tree_dirty() is False

    def test_the_local_commit_is_preserved(self, conflicted):
        """Abort, not reset --hard: an approval that committed but never
        pushed exists only here."""
        clone, local_head = conflicted
        with pytest.raises(gitrepo.BrainRepoError):
            gitrepo.pull_rebase()

        assert gitrepo.head_sha() == local_head
        assert (clone / "note.md").read_text(encoding="utf-8") == "ours\n"

    def test_the_error_explains_what_to_look_for(self, conflicted):
        with pytest.raises(gitrepo.BrainRepoError) as exc:
            gitrepo.pull_rebase()

        assert "rebase was aborted" in str(exc.value)
        assert "never pushed" in str(exc.value)

    def test_the_clone_is_still_readable(self, conflicted):
        with pytest.raises(gitrepo.BrainRepoError):
            gitrepo.pull_rebase()

        assert gitrepo.is_valid_repo() is True
        assert gitrepo.head_sha()


class TestAnAlreadyWedgedCloneHeals:
    def test_a_leftover_rebase_is_cleared_before_the_next_pull(self, conflicted):
        """An install already stuck when this shipped must recover by
        itself — the repair cannot require a human with a shell."""
        clone, _ = conflicted
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            cwd=str(clone), capture_output=True, text=True, encoding="utf-8",
        )
        assert gitrepo.rebase_in_progress() is True

        with pytest.raises(gitrepo.BrainRepoError):
            gitrepo.pull_rebase()

        assert gitrepo.rebase_in_progress() is False

    def test_a_pull_succeeds_once_the_conflict_is_gone(self, conflicted):
        clone, _ = conflicted
        with pytest.raises(gitrepo.BrainRepoError):
            gitrepo.pull_rebase()

        # The operator resolves it the obvious way: drop the local commit.
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=str(clone), capture_output=True, text=True, encoding="utf-8",
        )
        result = gitrepo.pull_rebase()

        assert result["changed"] == "true"
        assert (clone / "note.md").read_text(encoding="utf-8") == "theirs\n"

    def test_abort_rebase_is_a_no_op_on_a_healthy_clone(self, conflicted):
        assert gitrepo.abort_rebase() is False


class TestItIsVisibleToTheOperator:
    def test_status_probe_reports_it(self, conflicted):
        clone, _ = conflicted
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            cwd=str(clone), capture_output=True, text=True, encoding="utf-8",
        )

        assert gitrepo.status_probe()["rebase_in_progress"] is True

    def test_a_healthy_clone_reports_false(self, conflicted):
        assert gitrepo.status_probe()["rebase_in_progress"] is False

    def test_the_health_check_goes_danger(self, conflicted):
        from apps.brainconfig import health

        clone, _ = conflicted
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            cwd=str(clone), capture_output=True, text=True, encoding="utf-8",
        )

        check = health.check_clone()
        assert check["level"] == "danger"
        assert "mid-rebase" in check["title"]
        assert check["action"] == "pull_now"
