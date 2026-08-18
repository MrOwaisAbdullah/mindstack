"""Numeric settings must be total over what a text field can hold.

`Decimal("nan")` constructs without complaint — Python only raises
`InvalidOperation` when the NaN is *compared*. `daily_cost_cap()` did
`cap <= 0` outside its guard, so one stored "nan" 500'd every SDK run
AND the usage dashboard (both call it), and the operator's route to
fixing it — /ops/settings/ — renders the same accessor's output.

`_as_float` had the quieter sibling: `float("nan")` parses, and NaN
compares False with everything, so a NaN reader budget was a soft cap
that never tripped, silently. Infinity is "no cap" by stealth on both.

Rule pinned here: unparseable, non-finite, or cleared → the safe
default. Only an explicit `0` (or negative finite) disables.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.brainconfig import services

pytestmark = pytest.mark.django_db

DEFAULT_CAP = Decimal("10.00")


def _set(key: str, value: str) -> None:
    services.set_value(key, value)


@pytest.mark.parametrize("stored", ["nan", "NaN", "inf", "Infinity", "-inf", "snan"])
def test_non_finite_cap_falls_back_to_the_default(stored: str) -> None:
    _set("DAILY_COST_CAP", stored)
    assert services.daily_cost_cap() == DEFAULT_CAP


def test_garbage_cap_still_falls_back(stored: str = "ten dollars") -> None:
    _set("DAILY_COST_CAP", stored)
    assert services.daily_cost_cap() == DEFAULT_CAP


def test_zero_still_disables_explicitly() -> None:
    _set("DAILY_COST_CAP", "0")
    assert services.daily_cost_cap() is None


def test_a_real_cap_still_reads() -> None:
    _set("DAILY_COST_CAP", "25.50")
    assert services.daily_cost_cap() == Decimal("25.50")


@pytest.mark.parametrize("stored", ["nan", "inf", "-inf"])
def test_non_finite_budget_falls_back(stored: str) -> None:
    _set("MAX_BUDGET_USD_READER", stored)
    assert services.max_budget_usd("reader") == 0.50
