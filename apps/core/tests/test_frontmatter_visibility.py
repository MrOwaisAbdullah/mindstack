"""A file we cannot parse must never become more visible than it was.

Two ways `visibility: private` used to be silently downgraded to the path
default (`agents-only` for knowledge/identity/projects, `public` for
lenses and catalogs) — and then copied into that tier's snapshot and
served:

1. A UTF-8 BOM before the opening `---`. `_FRONTMATTER_RE` anchors on
   `\\A---`, and the file was decoded as plain utf-8, so the BOM survived
   and the block never matched at all. Notepad wrote BOMs by default for
   years, so this is a realistic way for a user to lose a tier.
2. Any YAML error inside the block — a tab indent, an unclosed quote.
   `safe_load` raised, `fm` became `{}`, and the explicit visibility went
   with it.

DB-free on purpose (see CLAUDE.md).
"""
from __future__ import annotations

import pytest

from apps.brain.services.indexer import (
    _resolve_visibility,
    parse_frontmatter,
    split_frontmatter,
)

PRIVATE_NOTE = "---\nid: secret\nvisibility: private\n---\n\n# Secret\n\nbody\n"


class TestParseFrontmatter:
    def test_clean_frontmatter(self):
        fm, body, malformed = parse_frontmatter(PRIVATE_NOTE)
        assert fm["visibility"] == "private"
        assert malformed is False
        assert "# Secret" in body

    def test_bom_no_longer_hides_the_block(self):
        """Decoding with utf-8-sig is what makes this parse."""
        raw = ("﻿" + PRIVATE_NOTE).encode("utf-8")
        fm, _body, malformed = parse_frontmatter(raw.decode("utf-8-sig"))
        assert fm["visibility"] == "private"
        assert malformed is False

    def test_bom_left_in_place_still_fails_to_match(self):
        """Pins WHY the decode matters: utf-8 alone keeps the BOM."""
        raw = ("﻿" + PRIVATE_NOTE).encode("utf-8")
        fm, _body, malformed = parse_frontmatter(raw.decode("utf-8"))
        assert fm == {}
        # No block matched, so this is "absent", not "malformed" — which is
        # exactly why the decode, not the malformed flag, is the fix here.
        assert malformed is False

    def test_no_frontmatter_at_all_is_not_malformed(self):
        """Catalogs legitimately have none — they must keep their default."""
        fm, body, malformed = parse_frontmatter("# Just a title\n\ntext\n")
        assert fm == {} and malformed is False and body.startswith("# Just")

    @pytest.mark.parametrize(
        "block",
        [
            "---\nid: x\n\tbad: tab indent\n---\nbody\n",
            '---\nid: "unclosed\n---\nbody\n',
            "---\n- a\n- list not a mapping\n---\nbody\n",
            "---\njust a string\n---\nbody\n",
        ],
    )
    def test_unparseable_block_is_flagged(self, block):
        fm, _body, malformed = parse_frontmatter(block)
        assert fm == {}
        assert malformed is True

    def test_legacy_two_tuple_wrapper_still_works(self):
        """graph.py unpacks two values."""
        fm, body = split_frontmatter(PRIVATE_NOTE)
        assert fm["visibility"] == "private" and "# Secret" in body


class TestResolveVisibility:
    def test_explicit_wins(self):
        assert _resolve_visibility("private", "public") == "private"
        assert _resolve_visibility("public", "agents-only") == "public"

    def test_missing_falls_back_to_the_path_default(self):
        assert _resolve_visibility(None, "agents-only") == "agents-only"
        assert _resolve_visibility("", "public") == "public"

    def test_unknown_value_falls_back_to_the_path_default(self):
        assert _resolve_visibility("agents_only", "agents-only") == "agents-only"

    @pytest.mark.parametrize("default", ["public", "agents-only", "private"])
    def test_malformed_fails_closed_regardless_of_default(self, default):
        """The finding: this used to return `default` and widen the note."""
        assert _resolve_visibility(None, default, malformed=True) == "private"
        assert _resolve_visibility("public", default, malformed=True) == "private"
