"""The sync pipeline: pull → drift check → reindex → snapshots.

Drift definition (PLAN.md §4, grill #2 discussion): the DB disagreeing
with the repo *at the same HEAD* — i.e. corruption or a parser change,
not a legitimately-ahead repo. Detected BEFORE the pull by comparing
state hashes when HEAD hasn't moved; always self-repaired by the rebuild
(repo wins) and logged as a `drift` Event so the dashboard health tile
goes loud instead of silently healing.

After every rebuild, a post-check asserts db_hash == repo_hash — if the
indexer itself can't reproduce the repo, the sync FAILS rather than
serving a wrong index.
"""
from __future__ import annotations

import logging

from apps.brain.models import SyncRun
from apps.brain.services import gitrepo, indexer, snapshots
from apps.events.models import emit

log = logging.getLogger(__name__)


class SyncError(RuntimeError):
    pass


def last_indexed_sha() -> str:
    run = SyncRun.objects.filter(ok=True).exclude(commit_sha="").first()
    return run.commit_sha if run else ""


def publish_snapshots(run: SyncRun) -> None:
    """Materialise every tier, and make a failure visible on the run.

    `indexer.rebuild()` records `ok = True` the moment the DB index is
    consistent, which is before any tier has been written. A snapshot
    build that then failed propagated to the caller but left that row
    green — so the dashboard health tile, and `last_indexed_sha()`,
    reported a brain that was published when it was not. Every path that
    reindexes goes through here so the row and the disk agree.
    """
    try:
        snapshots.build_all()
    except Exception as exc:
        run.ok = False
        run.error = (f"{run.error}; " if run.error else "") + f"snapshot build failed: {exc}"
        run.save(update_fields=["ok", "error"])
        emit("degraded", surface="snapshots", head=run.commit_sha, reason=str(exc)[:500])
        log.exception("brain: snapshot build failed at %s", (run.commit_sha or "")[:12])
        raise


def sync(trigger: str = "manual", *, pull: bool = True) -> SyncRun:
    """Full pipeline. Returns the SyncRun row (drift flag set when detected).

    pull=False skips the git pull for credential-less environments (the
    local stack bind-mounts a host-owned clone: the host pulls, the
    container only reindexes) — the contract check still runs.
    """
    pre_head = gitrepo.head_sha() if gitrepo.is_valid_repo() else ""
    drift = False
    drift_reason = ""

    # Drift check: only meaningful when the DB claims to be at this HEAD.
    if pre_head and last_indexed_sha() == pre_head:
        repo_hash = indexer.state_hash()
        db_hash = indexer.db_state_hash()
        if repo_hash != db_hash:
            drift = True
            drift_reason = "db state diverged from repo at same HEAD"
            log.warning("brain: DRIFT detected at %s — self-repairing", pre_head[:12])

    if pull:
        pulled = gitrepo.pull_rebase()["changed"]
    else:
        if not gitrepo.is_valid_repo():
            raise SyncError(f"No valid clone at {gitrepo.repo_dir()} — run bootstrap first.")
        gitrepo.verify_contract()
        pulled = "skipped"
    run = indexer.rebuild(trigger=trigger)

    # Post-check: the rebuilt DB must reproduce the repo exactly.
    if indexer.state_hash() != indexer.db_state_hash():
        run.ok = False
        run.error = "post-rebuild hash mismatch: indexer cannot reproduce repo state"
        run.save(update_fields=["ok", "error"])
        emit("drift", entity_ids=[], stage="post-rebuild", head=run.commit_sha)
        raise SyncError(run.error)

    publish_snapshots(run)

    if drift:
        run.drift_detected = True
        run.save(update_fields=["drift_detected"])
        emit("drift", stage="pre-pull", reason=drift_reason, head=pre_head, repaired=True)

    emit(
        "sync",
        trigger=trigger,
        head=run.commit_sha,
        pulled=pulled,
        added=run.added,
        changed=run.changed,
        removed=run.removed,
        drift=drift,
    )
    return run
