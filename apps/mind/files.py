"""Snapshot file access — the only door the serving layers read through.

Everything serves from `/data/brain-views/<tier>/` (PLAN.md §5): if a
file isn't in the caller's tier snapshot, it does not exist for that
caller. That single rule carries the whole visibility model — including
raw/ link-inheritance and the stripped public spans — because the
snapshot builder already enforced it at build time.
"""
from __future__ import annotations

from pathlib import Path

from apps.brain.services import snapshots


class SnapshotMiss(ValueError):
    """Requested path absent from the caller's tier snapshot."""


def read(tier: str, relpath: str, *, subdir: str | None = None) -> str:
    """Read `relpath` from the caller's tier snapshot.

    `subdir` additionally confines the read to one directory inside the
    snapshot. Callers that restrict by prefix (`get-raw` serves only
    `raw/`) must pass it: a `startswith("raw/")` test is satisfied by
    `raw/../INDEX.md`, which resolves back out of `raw/` and used to be
    served — skipping the per-entity `tiers.allows` check that `get-note`
    applies, since nothing about a raw read consults the DB.
    """
    base = snapshots.tier_dir(tier).resolve()
    root = (base / subdir).resolve() if subdir else base
    target = (base / relpath).resolve()
    # Traversal guard: the resolved target must stay inside the snapshot,
    # and inside `subdir` when the caller named one.
    if root not in target.parents and target != root:
        raise SnapshotMiss(f"unknown path: {relpath}")
    if not target.is_file():
        raise SnapshotMiss(f"unknown path: {relpath}")
    return target.read_text(encoding="utf-8", errors="replace")


def exists(tier: str, relpath: str, *, subdir: str | None = None) -> bool:
    try:
        read(tier, relpath, subdir=subdir)
        return True
    except SnapshotMiss:
        return False
