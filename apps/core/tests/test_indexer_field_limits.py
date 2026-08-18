"""One over-long frontmatter value must not freeze the whole index.

Nothing clamped parsed values before saving, so Postgres raised DataError
and aborted the entire rebuild — every note in the repo stopped updating
because of one long H1 or a `type:` that didn't fit 16 chars. SQLite (dev)
accepts over-length values silently, so this only ever bit in production:
exactly the prod-only divergence CLAUDE.md warns about.

DB-free on purpose (see CLAUDE.md).
"""
from __future__ import annotations

from apps.brain.models import Entity
from apps.brain.services.indexer import _MAX_LENGTHS, _fit_to_schema, ParsedEntity


def _e(**kw) -> ParsedEntity:
    base = dict(entity_id="an-id", kind="take", path="knowledge/takes/a.md")
    return ParsedEntity(**{**base, **kw})


def test_limits_are_read_off_the_model():
    """Pinned to the schema, so a migration can't silently outdate them."""
    assert _MAX_LENGTHS["title"] == Entity._meta.get_field("title").max_length
    assert _MAX_LENGTHS["kind"] == Entity._meta.get_field("kind").max_length
    assert _MAX_LENGTHS["date"] == Entity._meta.get_field("date").max_length


def test_long_title_is_truncated_not_fatal():
    kept = _fit_to_schema([_e(title="T" * 500)])
    assert len(kept) == 1
    assert len(kept[0].title) == _MAX_LENGTHS["title"]


def test_long_kind_and_date_are_truncated():
    """`type: recommendation-notes` (>16) and `date: January 2026` (>10)."""
    kept = _fit_to_schema([_e(kind="recommendation-notes", date="January 2026")])
    assert len(kept[0].kind) == _MAX_LENGTHS["kind"]
    assert len(kept[0].date) == _MAX_LENGTHS["date"]


def test_long_source_url_is_truncated():
    kept = _fit_to_schema([_e(source_url="https://x.test/" + "q" * 900)])
    assert len(kept[0].source_url) == _MAX_LENGTHS["source_url"]


def test_values_within_limits_are_untouched():
    original = _e(title="A normal title", kind="take", date="2026-08")
    kept = _fit_to_schema([original])
    assert kept[0].title == "A normal title"
    assert kept[0].kind == "take"
    assert kept[0].date == "2026-08"


def test_over_long_path_skips_the_file_rather_than_truncating():
    """Truncating a path would break every later read of that file."""
    kept = _fit_to_schema([_e(path="knowledge/takes/" + "d" * 400 + ".md")])
    assert kept == []


def test_over_long_entity_id_skips_the_file():
    """Truncating an id could collide with a different note's id."""
    assert _fit_to_schema([_e(entity_id="i" * 400)]) == []


def test_one_bad_file_does_not_drop_the_others():
    good_a, good_b = _e(entity_id="a", path="a.md"), _e(entity_id="b", path="b.md")
    bad = _e(entity_id="c" * 400, path="c.md")
    kept = _fit_to_schema([good_a, bad, good_b])
    assert [k.path for k in kept] == ["a.md", "b.md"]
