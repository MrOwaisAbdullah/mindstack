"""`raw/` must mean `raw/` — on the way in and on the way out.

Two independent holes, same root cause: a `raw/` *prefix* was treated as a
containment guarantee. `_RAW_REF_RE` accepts `.` and `/`, so a reference
like `raw/../identity/core.md` matched it, passed `is_file()`, and was
copied into whatever tier was building — including public. On the serving
side, `get-raw`'s `startswith("raw/")` test was equally satisfied by
`raw/../INDEX.md`, which then resolved back out of `raw/` and was served
without the DB tier check `get-note` applies.

DB-free on purpose (see CLAUDE.md).
"""
from __future__ import annotations

import pytest

from apps.brain.services.snapshots import _all_raw, _inside_raw


@pytest.fixture()
def repo(tmp_path):
    """A brain repo with a secret outside raw/ and a nested file inside it."""
    (tmp_path / "raw" / "interviews").mkdir(parents=True)
    (tmp_path / "identity").mkdir()
    (tmp_path / "raw" / "top.md").write_text("top level raw", encoding="utf-8")
    (tmp_path / "raw" / "interviews" / "2024-06.md").write_text("nested", encoding="utf-8")
    (tmp_path / "raw" / "README.md").write_text("skip me", encoding="utf-8")
    (tmp_path / "identity" / "core.md").write_text("PRIVATE", encoding="utf-8")
    return tmp_path


class TestInsideRaw:
    def test_accepts_real_raw_files_at_any_depth(self, repo):
        assert _inside_raw(repo, "raw/top.md")
        assert _inside_raw(repo, "raw/interviews/2024-06.md")

    def test_rejects_traversal_out_of_raw(self, repo):
        """The finding: a public note body containing this ref."""
        assert not _inside_raw(repo, "raw/../identity/core.md")
        assert not _inside_raw(repo, "raw/interviews/../../identity/core.md")

    def test_rejects_paths_outside_the_repo(self, repo):
        assert not _inside_raw(repo, "raw/../../etc/passwd")

    def test_rejects_directories_and_missing_files(self, repo):
        assert not _inside_raw(repo, "raw/interviews")
        assert not _inside_raw(repo, "raw/nope.md")

    def test_rejects_symlink_escaping_raw(self, repo):
        try:
            (repo / "raw" / "leak.md").symlink_to(repo / "identity" / "core.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this host")
        assert not _inside_raw(repo, "raw/leak.md")


class TestAllRaw:
    def test_includes_nested_files(self, repo):
        """Private tier used to miss these — glob('*.md') was top-level only."""
        assert _all_raw(repo) == {"raw/top.md", "raw/interviews/2024-06.md"}

    def test_skips_readme(self, repo):
        assert "raw/README.md" not in _all_raw(repo)

    def test_empty_when_no_raw_dir(self, tmp_path):
        assert _all_raw(tmp_path) == set()
