"""`files.read(..., subdir="raw")` is what stops get-raw escaping raw/.

`get-raw` keeps a `startswith("raw/")` test for the friendly error message,
but that test is satisfied by `raw/../INDEX.md`. Only resolving the path
and re-checking containment refuses it. This matters more than a tidy 404:
`get-raw` consults no DB at all, so anything it serves bypasses the
per-entity `tiers.allows` check that `get-note` applies.

DB-free on purpose (see CLAUDE.md).
"""
from __future__ import annotations

import pytest

from apps.mind import files


@pytest.fixture()
def snapshot(tmp_path, settings):
    """A public-tier snapshot with content inside and outside raw/."""
    settings.BRAIN_VIEWS_DIR = str(tmp_path)
    public = tmp_path / "public"
    (public / "raw" / "deep").mkdir(parents=True)
    (public / "notes").mkdir()
    (public / "raw" / "doc.md").write_text("raw doc", encoding="utf-8")
    (public / "raw" / "deep" / "nested.md").write_text("nested raw", encoding="utf-8")
    (public / "INDEX.md").write_text("the index", encoding="utf-8")
    (public / "_MANIFEST.json").write_text("{}", encoding="utf-8")
    (public / "notes" / "note.md").write_text("a note", encoding="utf-8")
    return public


def test_serves_files_inside_raw(snapshot):
    assert files.read("public", "raw/doc.md", subdir="raw") == "raw doc"
    assert files.read("public", "raw/deep/nested.md", subdir="raw") == "nested raw"


@pytest.mark.parametrize(
    "path",
    [
        "raw/../INDEX.md",
        "raw/../_MANIFEST.json",
        "raw/../notes/note.md",
        "raw/deep/../../INDEX.md",
    ],
)
def test_refuses_traversal_out_of_raw(snapshot, path):
    """Each of these passes startswith('raw/') and used to be served."""
    with pytest.raises(files.SnapshotMiss):
        files.read("public", path, subdir="raw")


def test_refuses_escape_from_the_snapshot_entirely(snapshot):
    with pytest.raises(files.SnapshotMiss):
        files.read("public", "raw/../../private/secret.md", subdir="raw")


def test_without_subdir_the_snapshot_root_still_bounds_the_read(snapshot):
    """Unchanged behaviour for get-note/get-index, which serve the whole tier."""
    assert files.read("public", "INDEX.md") == "the index"
    with pytest.raises(files.SnapshotMiss):
        files.read("public", "../elsewhere.md")


def test_exists_mirrors_read(snapshot):
    assert files.exists("public", "raw/doc.md", subdir="raw")
    assert not files.exists("public", "raw/../INDEX.md", subdir="raw")
