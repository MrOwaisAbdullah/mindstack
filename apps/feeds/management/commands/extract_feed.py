"""Run feeder extraction for one Feed inline (no worker round-trip).

Dev/ops escape hatch: the production path is propose → Q2 worker →
run_extraction; this command runs the same task function in-process so
extraction can be exercised or unstuck without a running qcluster.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.feeds.models import Feed
from apps.feeds.services import feeder


class Command(BaseCommand):
    help = "Run the feeder agent for one pending Feed, inline."

    def add_arguments(self, parser):
        parser.add_argument("feed_id", type=int)

    def handle(self, *args, **opts):
        feed = Feed.objects.filter(pk=opts["feed_id"]).first()
        if feed is None:
            raise CommandError(f"no Feed with id {opts['feed_id']}")
        self.stdout.write(f"extracting feed {feed.pk} ({feed.source_id})…")
        status = feeder.run_extraction(feed.pk)
        self.stdout.write(status)
        feed.refresh_from_db()
        if feed.proposal:
            self.stdout.write(self.style.SUCCESS(f"proposal stored: {len(feed.proposal.get('files', []))} file(s), {len(feed.proposal.get('issues', []))} issue(s)"))
        elif feed.error:
            self.stdout.write(self.style.ERROR(feed.error))
