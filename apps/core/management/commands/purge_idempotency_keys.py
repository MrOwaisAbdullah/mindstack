"""manage.py purge_idempotency_keys — daily TTL cron driver.

Default behavior: delete every `IdempotentRequest` row whose
`created_at < now - 24h`. Override with `--ttl-seconds N`. `--dry-run`
prints the count without touching the table.

Registered as a Q2 `Schedule` row via `config/scheduled.py` (applied
with `manage.py sync_scheduled`) alongside the other cleanup crons.
Operators can also run it manually.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Delete IdempotentRequest rows older than the TTL (default 24h)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--ttl-seconds",
            type=int,
            default=None,
            help="Override the 24h TTL (in seconds).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the candidate count without deleting.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.core.idempotency import DEFAULT_TTL_SECONDS, purge_expired
        from apps.core.models import IdempotentRequest

        # Explicit None check — `--ttl-seconds 0` is a valid override
        # (purge everything that's not literally newer than this instant).
        # `options.get(...) or DEFAULT` would silently swallow it.
        raw = options.get("ttl_seconds")
        ttl = DEFAULT_TTL_SECONDS if raw is None else int(raw)
        cutoff = timezone.now() - timedelta(seconds=ttl)

        if options.get("dry_run"):
            count = IdempotentRequest.objects.filter(created_at__lt=cutoff).count()
            self.stdout.write(
                f"DRY RUN — would delete {count} IdempotentRequest row(s) "
                f"older than {cutoff.isoformat()}"
            )
            return

        deleted = purge_expired(ttl_seconds=ttl)
        self.stdout.write(
            self.style.SUCCESS(  # type: ignore[attr-defined]
                f"purged {deleted} IdempotentRequest row(s) older than {cutoff.isoformat()}"
            )
        )
