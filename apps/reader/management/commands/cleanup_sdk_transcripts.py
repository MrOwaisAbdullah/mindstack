"""Delete Claude CLI session transcripts past their retention window.

The bundled CLI writes JSONL session files under ~/.claude/projects/…
inside every container that runs SDK agents. They contain note content
in plaintext (grill C3: sensitive), so they get a retention window, not
immortality.

This runs on a Q2 schedule (see `config/scheduled.py`); running it by hand
at any time is harmless. The implementation lives in
`apps.reader.scheduled` so the command and the schedule share one code
path.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.reader.scheduled import DEFAULT_RETENTION_DAYS, run_cleanup_sdk_transcripts


class Command(BaseCommand):
    help = "Delete SDK session transcripts older than --days (default 7)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)

    def handle(self, *args: object, **options: object) -> None:
        result = run_cleanup_sdk_transcripts(days=int(options["days"]))
        self.stdout.write(
            self.style.SUCCESS(
                f"removed {result['removed']} transcript file(s) older than {result['days']}d"
            )
        )
