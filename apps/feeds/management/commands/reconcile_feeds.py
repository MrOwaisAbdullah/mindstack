"""Resolve Feeds wedged in `approving`, from a shell.

The ops button and the scheduled beat both go through the worker queue.
This exists for when the queue itself is the problem — a dead broker is
one of the ways a feed gets wedged in the first place, and "the recovery
needs the thing that broke" is not a recovery.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.feeds.services import approval


class Command(BaseCommand):
    help = "Complete or release Feeds stuck in the `approving` status."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Include claims still inside the worker timeout. Only use this "
                "when you know no worker is running — otherwise a live approval "
                "can be judged mid-flight."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be reconciled and change nothing.",
        )

    def handle(self, *args, **options):
        feeds = approval.stuck_feeds(force=options["force"])
        if not feeds:
            self.stdout.write(self.style.SUCCESS("No stuck approvals."))
            return

        for feed in feeds:
            self.stdout.write(
                f"  feed {feed.pk} · {feed.source_id} · claimed "
                f"{feed.approve_claimed_at or 'at an unrecorded time'}"
            )
        if options["dry_run"]:
            self.stdout.write(f"{len(feeds)} would be reconciled (dry run).")
            return

        result = approval.reconcile_stuck(force=options["force"])
        self.stdout.write(
            self.style.SUCCESS(
                f"scanned {result['scanned']} · "
                f"recovered {result['recovered']} (already committed) · "
                f"returned to pending {result['returned']} · "
                f"skipped {result['skipped']} (a worker got there first)"
            )
        )
