"""An unrecognised stored tier must read as public, never as a 500.

`tier_for_credential` returned `Consumer.max_visibility` verbatim. The
model's `choices` only guard forms; a hand-edited row (or a value from a
future migration) can hold anything, and the unclamped string flowed
into `TIER_ORDER[tier]` in `_visible_entities` — a KeyError, i.e. a 500
on every `list-notes`/`get-identity` call that credential makes, forever.
Every sibling rank check (`tiers.allows`, the snapshot filter, the graph
ceiling) uses `.get(..., default)` and fails closed, and the URL-token
path clamps its stored tier on read (`url_mcp_tokens.api.tier_for`);
the APIKey path was the one hole. `reader._verify_entities` had the
same `[tier]` indexing.

Fail direction matters: unknown tier → *public* (rank 0, the least
access), matching the documented fallback everywhere else. Silently
granting more would be the P0-#5 bug shape again.
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User

from apps.api_keys.models import APIKey
from apps.brain.models import Entity
from apps.core.testing import call_endpoint
from apps.mind import tiers
from apps.mind.endpoints import ListNotes
from apps.mind.models import Consumer
from apps.reader.services import reader

pytestmark = pytest.mark.django_db


@pytest.fixture
def junk_tier_key():
    user = User.objects.create_user("tier-test", password="x" * 20)
    key = APIKey.objects.create(
        user=user, name="junk-tier", prefix="mcpsk_junk", key_hash="h" * 64, last_4="junk"
    )
    # Straight ORM write, as a hand-edited row would be — the ops UI's
    # validators are exactly what this value never went through.
    Consumer.objects.create(api_key=key, max_visibility="internal")
    return key


@pytest.fixture
def two_entities():
    Entity.objects.create(
        entity_id="pub-note", kind="take", path="notes/pub.md",
        title="Public note", visibility="public", content_hash="x",
    )
    Entity.objects.create(
        entity_id="priv-note", kind="take", path="notes/priv.md",
        title="Private note", visibility="private", content_hash="x",
    )


def test_unknown_stored_tier_resolves_as_public(junk_tier_key) -> None:
    assert tiers.tier_for_credential(junk_tier_key) == "public"


def test_list_notes_fails_closed_instead_of_500(junk_tier_key, two_entities) -> None:
    """The finding, end to end at the endpoint layer: unfixed, this
    raises KeyError('internal') out of `_visible_entities` — the REST
    view turns that into a 500 on every call."""
    out = async_to_sync(call_endpoint)(ListNotes, {}, credential=junk_tier_key)
    assert out.tier == "public"
    assert [n.entity_id for n in out.notes] == ["pub-note"]


def test_verify_entities_rejects_above_public_on_junk_tier(two_entities) -> None:
    """Reader-side twin: a junk tier verifies at public rank, so the
    private id is rejected — not a KeyError out of the verify step."""
    verified, rejected = reader._verify_entities("internal", ["pub-note", "priv-note"])
    assert verified == ["pub-note"]
    assert "priv-note" in rejected
