"""Renaming a note must not brick sync forever.

`Entity.entity_id` and `Entity.path` are each unique. `rebuild()` used to
create new paths before pruning old ones, so renaming a file while keeping
its frontmatter `id:` — the normal case, since `id` is the stable identity
— collided with the old row's `entity_id`. There was no transaction, so
rows written before the collision stayed committed and the index was left
half-migrated. Every retry then failed at the same file, and "Replace the
clone" could not repair it because that path re-runs this same function.
"""
from __future__ import annotations

import pytest

from apps.brain.models import Entity, SyncRun
from apps.brain.services import indexer
from apps.brain.services.indexer import ParsedEntity


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    monkeypatch.setattr(indexer.gitrepo, "head_sha", lambda: "0" * 40)


def _parse(monkeypatch, entities):
    monkeypatch.setattr(indexer, "parse_repo", lambda: entities)


def _e(entity_id: str, path: str, **kw) -> ParsedEntity:
    return ParsedEntity(entity_id=entity_id, kind="take", path=path, **kw)


@pytest.mark.django_db
def test_rename_keeping_the_same_id(monkeypatch):
    """The finding. This used to raise IntegrityError and stay broken."""
    _parse(monkeypatch, [_e("my-take", "knowledge/takes/x.md", title="X")])
    indexer.rebuild()

    _parse(monkeypatch, [_e("my-take", "knowledge/takes/y.md", title="X")])
    run = indexer.rebuild()

    assert run.ok is True
    rows = list(Entity.objects.all())
    assert len(rows) == 1
    assert rows[0].path == "knowledge/takes/y.md"
    assert rows[0].entity_id == "my-take"


@pytest.mark.django_db
def test_rename_across_folders(monkeypatch):
    _parse(monkeypatch, [_e("t", "knowledge/takes/a.md")])
    indexer.rebuild()

    _parse(monkeypatch, [_e("t", "knowledge/lessons/a.md")])
    assert indexer.rebuild().ok is True
    assert Entity.objects.get(entity_id="t").path == "knowledge/lessons/a.md"


@pytest.mark.django_db
def test_two_files_swapping_ids(monkeypatch):
    """Both paths survive the prune, so the unique columns collide head-on."""
    _parse(monkeypatch, [_e("one", "a.md"), _e("two", "b.md")])
    indexer.rebuild()

    _parse(monkeypatch, [_e("two", "a.md"), _e("one", "b.md")])
    assert indexer.rebuild().ok is True

    assert Entity.objects.get(path="a.md").entity_id == "two"
    assert Entity.objects.get(path="b.md").entity_id == "one"


@pytest.mark.django_db
def test_new_file_adopting_a_surviving_rows_id(monkeypatch):
    _parse(monkeypatch, [_e("shared", "old.md")])
    indexer.rebuild()

    # old.md keeps its path but hands its id to a new file.
    _parse(monkeypatch, [_e("renamed", "old.md"), _e("shared", "new.md")])
    assert indexer.rebuild().ok is True

    assert Entity.objects.get(path="old.md").entity_id == "renamed"
    assert Entity.objects.get(path="new.md").entity_id == "shared"


@pytest.mark.django_db
def test_duplicate_id_is_reported_as_itself(monkeypatch):
    """A repo authoring error, not a transient fault — name the files."""
    _parse(monkeypatch, [_e("dupe", "a.md"), _e("dupe", "b.md")])

    with pytest.raises(indexer.DuplicateEntityId) as exc:
        indexer.rebuild()

    assert "dupe" in str(exc.value)
    assert "a.md" in str(exc.value) and "b.md" in str(exc.value)


@pytest.mark.django_db
def test_a_failed_rebuild_leaves_the_index_untouched(monkeypatch):
    """No half-migrated state: the upsert is one transaction."""
    _parse(monkeypatch, [_e("keep", "keep.md", title="original")])
    indexer.rebuild()

    boom = [_e("keep", "keep.md", title="edited"), _e("dupe", "x.md"), _e("dupe", "y.md")]
    _parse(monkeypatch, boom)
    with pytest.raises(indexer.DuplicateEntityId):
        indexer.rebuild()

    assert Entity.objects.count() == 1
    assert Entity.objects.get(path="keep.md").title == "original"


@pytest.mark.django_db
def test_failed_rebuild_is_recorded_on_the_run(monkeypatch):
    _parse(monkeypatch, [_e("dupe", "a.md"), _e("dupe", "b.md")])
    with pytest.raises(indexer.DuplicateEntityId):
        indexer.rebuild()

    run = SyncRun.objects.latest("id")
    assert run.ok is False
    assert "duplicate frontmatter id" in run.error


@pytest.mark.django_db
def test_deletion_still_prunes(monkeypatch):
    _parse(monkeypatch, [_e("a", "a.md"), _e("b", "b.md")])
    indexer.rebuild()

    _parse(monkeypatch, [_e("a", "a.md")])
    run = indexer.rebuild()

    assert run.removed == 1
    assert list(Entity.objects.values_list("path", flat=True)) == ["a.md"]


@pytest.mark.django_db
def test_counts_distinguish_added_changed_and_reidentified(monkeypatch):
    _parse(monkeypatch, [_e("a", "a.md", title="one")])
    first = indexer.rebuild()
    assert (first.added, first.changed, first.removed) == (1, 0, 0)

    # a.md changes id (re-identified, not new), b.md is genuinely new.
    _parse(monkeypatch, [_e("a2", "a.md", title="one"), _e("b", "b.md")])
    second = indexer.rebuild()
    assert (second.added, second.changed) == (1, 1)


@pytest.mark.django_db
def test_unchanged_rebuild_is_a_no_op(monkeypatch):
    ents = [_e("a", "a.md", title="t")]
    _parse(monkeypatch, ents)
    indexer.rebuild()
    run = indexer.rebuild()

    assert (run.added, run.changed, run.removed) == (0, 0, 0)
