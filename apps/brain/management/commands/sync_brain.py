"""Manual sync: pull → drift check → reindex → snapshots.

PLAN.md §4 describes a 15-minute fallback beat scheduling this via
django-q2 at deploy. **There is no such beat.** It would be registered
from `config/scheduled.py`, and that file is not in the repo, so
`manage.py sync_scheduled` has nothing to declare — a real deploy comes
up with zero Q2 `Schedule` rows (measured on a fresh prod stack).

So this command is the operator's by-hand sync, and along with the
GitHub webhook and the "Pull from GitHub" button on `/ops/health/` it is
one of only three things that will ever move the clone forward. See
docs/DEPLOY.md §7.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.brain.services import gitrepo, sync


class Command(BaseCommand):
    help = "Run the full brain sync pipeline (pull, reindex, snapshots, drift check)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--trigger", default="manual", choices=["manual", "beat"])
        parser.add_argument(
            "--no-pull",
            action="store_true",
            help="Skip the git pull (credential-less local dev: the host "
            "owns the pull; this reindexes + rebuilds snapshots only).",
        )

    def handle(self, *args: object, **options: object) -> None:
        try:
            run = sync.sync(trigger=str(options["trigger"]), pull=not options["no_pull"])
        except (gitrepo.BrainRepoError, sync.SyncError) as exc:
            raise CommandError(str(exc)) from exc
        flag = " [DRIFT repaired]" if run.drift_detected else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"synced at {run.commit_sha[:12]}: +{run.added} ~{run.changed} "
                f"-{run.removed} in {run.duration_ms}ms{flag}"
            )
        )
