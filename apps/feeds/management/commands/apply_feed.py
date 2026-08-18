"""Run the approval handler for one Feed inline (no worker round-trip).

Dev/ops escape hatch mirroring `extract_feed`: claims the pending Feed
(pending → approving) and runs the same locked apply sequence the Q2
worker would. Refuses invalid proposals exactly like the UI does.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.feeds.models import Feed
from apps.feeds.services import approval, validator


class Command(BaseCommand):
    help = "Approve + apply one pending Feed inline (commit + push)."

    def add_arguments(self, parser):
        parser.add_argument("feed_id", type=int)

    def handle(self, *args, **opts):
        feed = Feed.objects.filter(pk=opts["feed_id"]).first()
        if feed is None:
            raise CommandError(f"no Feed with id {opts['feed_id']}")
        res = validator.validate_feed(feed)
        if not res.valid:
            raise CommandError(
                "proposal fails validation:\n" + "\n".join(str(v) for v in res.violations)
            )
        claimed = Feed.objects.filter(pk=feed.pk, status="pending").update(status="approving")
        if not claimed:
            raise CommandError(f"Feed is {feed.status} — only pending feeds can be approved")
        self.stdout.write(f"applying feed {feed.pk} ({feed.source_id})…")
        self.stdout.write(approval.apply_feed(feed.pk))
