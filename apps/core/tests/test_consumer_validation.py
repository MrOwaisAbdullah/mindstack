"""Input rules for `/ops/consumers/` (the API-keys page).

DB-free on purpose (CLAUDE.md: the host venv has no `django_redis`, so
`django_db` cannot stand up a cache backend here). The two-row
invariants these validators guard — a minted key gets a profile, a
rotated key carries its old one — need the DB and are exercised against
the running stack instead.

The validators matter because the tier string reaches
`tiers.tier_for_credential` unmediated: an unrecognised value there does
not raise, it falls through `TIER_ORDER.get(tier, 0)` to *public*. A
typo would silently mint a key with less access than the operator asked
for, and the page would then render the typo back as if it were real.
"""
from __future__ import annotations

import pytest

from apps.mind.consumers import (
    MAX_RATE_LIMIT,
    MIN_RATE_LIMIT,
    TIER_HELP,
    TIERS,
    InvalidConsumer,
    clean_name,
    clean_rate_limit,
    clean_tier,
)
from apps.mind.tiers import TIER_ORDER


def test_the_offered_tiers_are_exactly_the_ones_enforcement_understands():
    """The picker cannot offer a tier the read path would treat as public."""
    assert set(TIERS) == set(TIER_ORDER)


def test_every_offered_tier_has_help_text():
    """The operator is granting an agent access to their own private notes;
    a bare enum value is not enough to choose on."""
    assert set(TIER_HELP) == set(TIERS)
    assert all(TIER_HELP[t].strip() for t in TIERS)


@pytest.mark.parametrize("tier", ["public", "agents-only", "private"])
def test_valid_tiers_pass_through(tier):
    assert clean_tier(tier) == tier


@pytest.mark.parametrize("bad", ["", "  ", "PRIVATE", "agents_only", "admin", None])
def test_unknown_tier_is_rejected_not_silently_downgraded(bad):
    with pytest.raises(InvalidConsumer):
        clean_tier(bad)


def test_name_is_required():
    with pytest.raises(InvalidConsumer):
        clean_name("   ")


def test_name_is_trimmed_and_capped_to_the_column():
    assert clean_name("  laptop  ") == "laptop"
    assert len(clean_name("x" * 500)) == 120


@pytest.mark.parametrize("raw", ["1", "60", 60, " 240 ", str(MAX_RATE_LIMIT)])
def test_sane_rate_limits_pass(raw):
    assert MIN_RATE_LIMIT <= clean_rate_limit(raw) <= MAX_RATE_LIMIT


@pytest.mark.parametrize("bad", ["0", "-5", str(MAX_RATE_LIMIT + 1), "sixty", "", None, "1.5"])
def test_out_of_range_or_non_numeric_rate_limit_is_rejected(bad):
    """Zero is the interesting one: it would be a key that exists and can
    never be used, which is what revoke is for. Letting it through would
    produce a credential that fails in a way nothing on the page explains."""
    with pytest.raises(InvalidConsumer):
        clean_rate_limit(bad)
