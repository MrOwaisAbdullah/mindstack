"""Scheduled-task callables for `apps.feeds`.

Declared in `config/scheduled.py`. Top-level callables; Q2 imports them by
dotted path from a fresh worker process.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run_reconcile_approvals() -> dict:
    """Resolve Feeds wedged in `approving`.

    Doubles as the ops job body behind `maintenance.RECONCILE_APPROVALS`,
    so the scheduled beat and the button on a stuck feed run exactly the
    same code — and the button gets the Tasks-page progress record every
    background action in this project is required to have.

    Runs on a beat as well as on demand because the failure it repairs
    (a worker killed mid-apply, losing the task outright — Q2 on the
    Redis broker has no ack) tends to happen during a deploy, which is
    precisely when nobody is watching the feed queue.
    """
    from apps.brainconfig.jobs import run
    from apps.brainconfig.maintenance import RECONCILE_APPROVALS
    from apps.feeds.services import approval

    def body(progress):
        progress(0, 1, "Checking the brain for commits from lost approvals")
        result = approval.reconcile_stuck()
        if not result["scanned"]:
            return "No stuck approvals."
        return (
            f"{result['scanned']} stuck · {result['recovered']} already committed · "
            f"{result['returned']} returned to pending"
        )

    run(RECONCILE_APPROVALS.name, RECONCILE_APPROVALS.label, body)
    return {"ok": True}


__all__ = ["run_reconcile_approvals"]
