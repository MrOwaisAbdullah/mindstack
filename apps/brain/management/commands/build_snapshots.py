"""Materialize the per-tier snapshots from the current clone + index."""
from django.core.management.base import BaseCommand, CommandError

from apps.brain.services import gitrepo, snapshots


class Command(BaseCommand):
    help = "Build /data/brain-views/{public,agents-only,private} snapshots."

    def handle(self, *args: object, **options: object) -> None:
        try:
            result = snapshots.build_all()
        except gitrepo.BrainRepoError as exc:
            raise CommandError(str(exc)) from exc
        for tier, mani in result.items():
            self.stdout.write(
                self.style.SUCCESS(
                    f"{tier}: {mani['entity_count']} entities, "
                    f"{len(mani['files'])} files at {str(mani['head'])[:12]}"
                )
            )
