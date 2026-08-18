"""Staleness — reader rule 3 (CLAUDE.md §6), in one place.

A project card whose `last-verified` is older than 45 days must not have
its specifics cited as current. The ops UI flags it; the graph view
carries it as a node attribute. Both read the rule from here so they can
never disagree about what "stale" means.
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

STALE_AFTER_DAYS = 45


def stale_cutoff() -> dt.date:
    return timezone.localdate() - dt.timedelta(days=STALE_AFTER_DAYS)


def is_stale(entity) -> bool:
    """Only project cards carry `last-verified`, so only they go stale."""
    if entity.kind != "project":
        return False
    return entity.last_verified is None or entity.last_verified < stale_cutoff()
