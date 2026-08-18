"""The write door's containment check must actually contain.

`_safe_target` is the only guard standing between a proposal's declared
path and `write_text` on the worker, and it compared resolved *strings*
with `startswith`. That is a prefix test, not containment: with the clone
at `<tmp>/brain-repo`, the path `../brain-repo-backup/note.md` resolves to
a wholly different directory that happens to share the first N characters,
and it passed. It also happily admitted `.git/`, which sits inside the
boundary — a proposal writing `.git/config` or `.git/hooks/pre-commit`
turns the next `gitrepo.run()` in this same module into code execution.

The validator's rule 5 rejects both shapes, but it is a gate; this is the
lock. DB-free on purpose (see CLAUDE.md).
"""
from __future__ import annotations

import pytest

from apps.feeds.services.approval import ApplyFailure, _apply_files, _safe_target


@pytest.fixture()
def repo(tmp_path):
    """A clone with a same-prefix sibling directory beside it."""
    clone = tmp_path / "brain-repo"
    (clone / "knowledge" / "takes").mkdir(parents=True)
    (clone / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "brain-repo-backup").mkdir()
    return clone


class TestSafeTarget:
    def test_accepts_an_ordinary_content_path(self, repo):
        assert _safe_target(repo, "knowledge/takes/x.md") == repo / "knowledge/takes/x.md"

    def test_accepts_a_path_whose_parents_do_not_exist_yet(self, repo):
        assert _safe_target(repo, "projects/new/card.md").is_relative_to(repo)

    def test_rejects_the_same_prefix_sibling(self, repo):
        """The finding. `startswith` said this was inside the clone."""
        with pytest.raises(ApplyFailure, match="escapes the repo"):
            _safe_target(repo, "../brain-repo-backup/note.md")

    def test_rejects_plain_traversal(self, repo):
        with pytest.raises(ApplyFailure, match="escapes the repo"):
            _safe_target(repo, "../../elsewhere/note.md")

    def test_rejects_traversal_that_returns_inside_a_sibling(self, repo):
        with pytest.raises(ApplyFailure, match="escapes the repo"):
            _safe_target(repo, "knowledge/../../brain-repo-backup/note.md")

    def test_rejects_an_absolute_path(self, repo, tmp_path):
        with pytest.raises(ApplyFailure, match="escapes the repo"):
            _safe_target(repo, str(tmp_path / "brain-repo-backup" / "note.md"))

    def test_rejects_the_repo_root_itself(self, repo):
        for relpath in ("", ".", "knowledge/.."):
            with pytest.raises(ApplyFailure, match="escapes the repo"):
                _safe_target(repo, relpath)

    def test_rejects_git_internals(self, repo):
        for relpath in (".git/config", ".git/hooks/pre-commit", "knowledge/../.git/config"):
            with pytest.raises(ApplyFailure, match="git internals"):
                _safe_target(repo, relpath)

    def test_rejects_case_folded_git_internals(self, repo):
        """On Windows and macOS `.GIT/config` is the same file."""
        with pytest.raises(ApplyFailure, match="git internals"):
            _safe_target(repo, ".GIT/config")

    def test_rejects_a_symlink_pointing_out_of_the_clone(self, repo, tmp_path):
        try:
            (repo / "knowledge" / "out").symlink_to(tmp_path / "brain-repo-backup")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this host")
        with pytest.raises(ApplyFailure, match="escapes the repo"):
            _safe_target(repo, "knowledge/out/note.md")


class TestApplyFilesRefusesToWrite:
    """The guard has to fire before the bytes land, not after."""

    def test_escaping_file_is_never_written(self, repo, tmp_path):
        proposal = {"files": [{"path": "../brain-repo-backup/note.md", "content": "leaked"}]}
        with pytest.raises(ApplyFailure):
            _apply_files(repo, proposal)
        assert not (tmp_path / "brain-repo-backup" / "note.md").exists()

    def test_git_config_is_never_written(self, repo):
        proposal = {"files": [{"path": ".git/config", "content": "[core]\n\tpager = sh -c evil\n"}]}
        with pytest.raises(ApplyFailure):
            _apply_files(repo, proposal)
        assert not (repo / ".git" / "config").exists()

    def test_a_legitimate_file_still_lands(self, repo):
        proposal = {"files": [{"path": "knowledge/takes/ok.md", "content": "body"}]}
        _apply_files(repo, proposal)
        assert (repo / "knowledge" / "takes" / "ok.md").read_text(encoding="utf-8") == "body\n"
