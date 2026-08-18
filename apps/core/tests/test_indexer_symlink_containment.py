"""A symlink in a content directory read files outside the repo.

`parse_repo()` finds notes with `glob("*.md")` in five content
directories. `glob` matches symlinks, and `read_bytes()` follows them —
so `identity/leak.md -> /data/state/boot-secrets.json` was read, parsed,
indexed, and published into whatever tier its (absent) frontmatter
implied. `raw/` was never exposed this way: `_inside_raw()` in
snapshots.py resolves before accepting a path, and has since the same
class of bug was fixed there. The indexer walk never got the treatment.

Planting the link needs write access to the brain repo, so this is not
reachable by an API caller — the operator, or whoever has taken their
git host. That is exactly why it is worth closing: the brain repo holds
notes, and the container holds the deploy key, the write PAT and
`boot-secrets.json`. "Someone can write to my notes" must not escalate
to "...and read the credentials that push to it". Those are separate
credentials on purpose (SECURITY.md), and a symlink was a way around the
separation.

Skip and warn rather than fail the sync: one bad link should not stop the
brain being served, and a sync that dies on a file the operator cannot
see named in an error is worse than one that carries on and says why.
"""
from __future__ import annotations

import logging

import pytest

from apps.brain.services import indexer

pytestmark = pytest.mark.django_db


def _repo(tmp_path, settings):
    repo = tmp_path / "repo"
    for d in ("knowledge/takes", "projects", "identity", "lenses", "content-catalog", "raw"):
        (repo / d).mkdir(parents=True, exist_ok=True)
    (repo / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
    settings.BRAIN_REPO_DIR = repo
    return repo


def _note(text: str = "real note") -> str:
    return f"---\nid: real\nvisibility: public\n---\n\n# {text}\n"


def _link(target, link_path) -> None:
    try:
        link_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")


class TestASymlinkOutOfTheRepoIsNotIndexed:
    @pytest.mark.parametrize(
        "folder", ["knowledge/takes", "projects", "identity", "lenses", "content-catalog"]
    )
    def test_every_content_directory_refuses_it(self, tmp_path, settings, folder) -> None:
        """All five globs, because the bug was in the pattern, not in one
        of its call sites."""
        repo = _repo(tmp_path, settings)
        secret = tmp_path / "boot-secrets.json"
        secret.write_text('{"FIELD_ENCRYPTION_KEY": "SHOULD-NOT-BE-INDEXED"}', encoding="utf-8")
        _link(secret, repo / folder / "leak.md")

        paths = [e.path for e in indexer.parse_repo()]

        assert not any("leak.md" in p for p in paths), (
            f"a symlink in {folder} was indexed: {paths}"
        )

    def test_the_skip_is_logged_with_the_path(self, tmp_path, settings, caplog) -> None:
        repo = _repo(tmp_path, settings)
        secret = tmp_path / "outside.md"
        secret.write_text("secret\n", encoding="utf-8")
        _link(secret, repo / "identity" / "leak.md")

        with caplog.at_level(logging.WARNING, logger=indexer.log.name):
            indexer.parse_repo()

        assert "leak.md" in caplog.text

    def test_a_symlinked_directory_is_refused_too(self, tmp_path, settings) -> None:
        """`identity -> /etc` is the same bug one level up."""
        repo = _repo(tmp_path, settings)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "leak.md").write_text("---\nid: leak\n---\n\n# leak\n", encoding="utf-8")
        (repo / "lenses").rmdir()
        _link(outside, repo / "lenses")

        paths = [e.path for e in indexer.parse_repo()]

        assert not any("leak.md" in p for p in paths), paths


class TestOrdinaryContentIsUnaffected:
    """Regression guards — the fix must not cost the normal case."""

    def test_a_real_note_is_still_indexed(self, tmp_path, settings) -> None:
        repo = _repo(tmp_path, settings)
        (repo / "knowledge" / "takes" / "real.md").write_text(_note(), encoding="utf-8")

        assert [e.path for e in indexer.parse_repo()] == ["knowledge/takes/real.md"]

    def test_a_symlink_pointing_inside_the_repo_still_works(self, tmp_path, settings) -> None:
        """Containment, not a ban on symlinks. A link that stays inside
        the brain resolves inside the brain and is ordinary content."""
        repo = _repo(tmp_path, settings)
        (repo / "knowledge" / "takes" / "real.md").write_text(_note(), encoding="utf-8")
        _link(repo / "knowledge" / "takes" / "real.md", repo / "identity" / "alias.md")

        paths = [e.path for e in indexer.parse_repo()]

        assert "identity/alias.md" in paths, paths

    def test_a_broken_symlink_is_skipped_not_fatal(self, tmp_path, settings) -> None:
        repo = _repo(tmp_path, settings)
        (repo / "knowledge" / "takes" / "real.md").write_text(_note(), encoding="utf-8")
        _link(tmp_path / "nothing-here.md", repo / "identity" / "dangling.md")

        paths = [e.path for e in indexer.parse_repo()]

        assert paths == ["knowledge/takes/real.md"], paths
