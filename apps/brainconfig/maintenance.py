"""Repair actions — the buttons that fix what the health panel reports.

Design rule for every one of these (SETUP-DESIGN.md build order #4):
**surface the real error, never a swallowed one.** Everything that can
fail here fails outside our process — git talking to GitHub, a clone that
turns out not to be a brain, a worker that isn't running. Replacing those
messages with "something went wrong" would leave an operator with no move
except to read our source, which is exactly the situation the setup
rebuild exists to remove. So each job returns, or raises with, the
underlying text.

The slow ones run on the worker rather than in the request: a clone
against a cold remote can take a minute, and a request that times out
mid-clone would leave the UI unable to say whether it worked.
"""
from __future__ import annotations

import logging

from apps.brainconfig.jobs import JobSpec, run, update

log = logging.getLogger(__name__)

VERIFY_READ = JobSpec(
    name="verify_read",
    label="Verify read access",
    task="apps.brainconfig.maintenance.job_verify_read",
)
PULL_NOW = JobSpec(
    name="pull_now",
    label="Pull from GitHub",
    task="apps.brainconfig.maintenance.job_pull_now",
)
REBUILD_INDEX = JobSpec(
    name="rebuild_index",
    label="Rebuild index and snapshots",
    task="apps.brainconfig.maintenance.job_rebuild_index",
)
REPLACE_CLONE = JobSpec(
    name="replace_clone",
    label="Replace the clone",
    task="apps.brainconfig.maintenance.job_replace_clone",
    danger=(
        "This deletes the server's copy of your brain and clones it again "
        "from the configured URL. Anything committed here but never pushed "
        "is lost. It will refuse if that is the case."
    ),
)
SETUP_BUILD = JobSpec(
    name="setup_build",
    label="Build the brain",
    task="apps.brainconfig.setup_services.run_build",
)
RECONCILE_APPROVALS = JobSpec(
    name="reconcile_approvals",
    label="Recover stuck approvals",
    task="apps.feeds.scheduled.run_reconcile_approvals",
)

#: Everything the Tasks page knows how to display.
ALL_JOBS = (
    SETUP_BUILD,
    VERIFY_READ,
    PULL_NOW,
    REBUILD_INDEX,
    REPLACE_CLONE,
    RECONCILE_APPROVALS,
)

#: What the health page is allowed to start, by name.
RUNNABLE = {j.name: j for j in (VERIFY_READ, PULL_NOW, REBUILD_INDEX, REPLACE_CLONE)}


# ---- job bodies ----------------------------------------------------------


def job_verify_read() -> None:
    """Clone into a scratch directory and report what git said.

    The setup wizard's Verify button runs this too. It used to call
    `verify_read_access` inline in the web request — a 180s git timeout
    under a 60s gunicorn worker timeout, on the one path where the remote
    is least likely to answer promptly, because the operator is usually
    still installing the deploy key when they press it. The worker killed
    the request mid-clone, the page never came back, and nothing recorded
    whether the clone had in fact succeeded.

    The structured half of the outcome is stashed on the job record.
    `run()` carries a label and an error string, and the wizard renders
    more than that: git's own stderr verbatim, and the contract files a
    reachable-but-not-a-brain repository is missing.
    """
    def body(progress):
        from apps.brain.services import gitrepo
        from apps.brainconfig import setup_services, setup_state

        url = gitrepo.configured_url()
        progress(0, 1, f"Cloning {url} into a scratch directory")
        outcome = setup_services.verify_read_access(url)
        update(
            VERIFY_READ.name,
            message=outcome.message,
            git_error=outcome.git_error,
            missing=list(outcome.missing_contract),
            head=outcome.head[:12],
        )
        if outcome.ok:
            setup_state.mark_read_verified()
            return f"Read access confirmed at {outcome.head[:12]}."
        detail = outcome.git_error or ", ".join(outcome.missing_contract)
        raise RuntimeError(f"{outcome.message} {detail}".strip())

    run(VERIFY_READ.name, VERIFY_READ.label, body)


def job_pull_now() -> None:
    def body(progress):
        from apps.brain.services import sync

        progress(0, 1, "Fetching from origin")
        result = sync.sync(trigger="manual")
        return (
            f"{result.commit_sha[:12]} · +{result.added} ~{result.changed} "
            f"-{result.removed}"
        )

    run(PULL_NOW.name, PULL_NOW.label, body)


def job_rebuild_index() -> None:
    def body(progress):
        from apps.brain.services import indexer, sync

        progress(0, 2, "Reading frontmatter")
        result = indexer.rebuild(trigger="manual")
        progress(1, 2, "Materialising snapshots")
        # Via sync so a snapshot failure marks the SyncRun not-ok — the
        # index alone being consistent is not the brain being published.
        sync.publish_snapshots(result)
        return f"+{result.added} ~{result.changed} -{result.removed} at {result.commit_sha[:12]}"

    run(REBUILD_INDEX.name, REBUILD_INDEX.label, body)


def job_replace_clone() -> None:
    def body(progress):
        from apps.brain.services import gitrepo, indexer, sync

        progress(0, 2, "Re-cloning from the configured URL")
        result = gitrepo.replace_clone()
        progress(1, 2, "Re-indexing")
        sync.publish_snapshots(indexer.rebuild(trigger="replace-clone"))
        return f"Re-cloned at {result['head'][:12]}."

    run(REPLACE_CLONE.name, REPLACE_CLONE.label, body)
