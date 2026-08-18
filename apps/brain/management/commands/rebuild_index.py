"""Rebuild the Entity index from the clone. Repo wins, always."""
from django.core.management.base import BaseCommand, CommandError

from apps.brain.models import Entity
from apps.brain.services import gitrepo, indexer


class Command(BaseCommand):
    help = "Full rescan of the brain clone into the Entity index."

    def handle(self, *args: object, **options: object) -> None:
        try:
            run = indexer.rebuild(trigger="manual")
        except gitrepo.BrainRepoError as exc:
            raise CommandError(str(exc)) from exc
        counts = dict(
            Entity.objects.values_list("kind").annotate(
                n=__import__("django.db.models", fromlist=["Count"]).Count("id")
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"index rebuilt at {run.commit_sha[:12]}: "
                f"+{run.added} ~{run.changed} -{run.removed} in {run.duration_ms}ms"
            )
        )
        for kind in sorted(counts):
            self.stdout.write(f"  {kind}: {counts[kind]}")
