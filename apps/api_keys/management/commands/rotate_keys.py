"""`manage.py rotate_keys <email>` — emergency revocation utility.

Revokes every active credential belonging to a single user: bearer API
keys AND the `mcpurl_` URL-path MCP tokens. The trigger is a leak — a
credential shows up in a paste site / a commit / a shared screenshot and
you want everything dead now, without deciding which one it was.

URL tokens are covered *because* of that trigger, not as an afterthought.
They are the credential most likely to leak: the whole secret is in a URL
that gets pasted into a hosted client, and a panic button that left the
claude.ai connector alive would be worse than useless — it would report
success while the most exposed credential kept working.

Revocation is immediate for both. API keys bust their cached Principal on
revoke rather than waiting out the 60s TTL; URL tokens are never cached
at all.

This stays a CLI tool rather than an ops-console button because it is an
"I know what I'm doing" operation: no confirmation, no per-credential
choice, just everything for one user gone. The click-driven paths are
`/<ops-prefix>/consumers/` for keys and `/<ops-prefix>/connectors/` for
connector URLs.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Revoke every active API key and URL-path MCP token for a single "
        "user (emergency procedure)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("email", help="Email of the user whose keys to revoke.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be revoked without actually revoking.",
        )

    def handle(self, *args: object, email: str, dry_run: bool, **options: object) -> None:
        # Late-import the api surface so this module is importable without
        # apps.api_keys ready (e.g. during a manage.py check) — keeps the
        # error message helpful instead of an obscure import error.
        from apps.api_keys import api as api_keys
        from apps.url_mcp_tokens import api as url_tokens

        User = get_user_model()
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(f"No user with email {email!r}.")

        active_keys = [
            k for k in api_keys.list_for_user(user) if k.revoked_at is None
        ]
        active_tokens = [
            t for t in url_tokens.list_for_user(user) if t.revoked_at is None
        ]
        if not active_keys and not active_tokens:
            self.stdout.write(
                self.style.WARNING(f"{email}: no active keys or connector URLs.")
            )
            return

        self.stdout.write(f"User: {user.email} (id={user.pk})")
        self.stdout.write(f"Active API keys to revoke ({len(active_keys)}):")
        for k in active_keys:
            self.stdout.write(f"  - {k.prefix}…{k.last_4}  name={k.name!r}  last_used={k.last_used_at}")
        self.stdout.write(f"Active connector URLs to revoke ({len(active_tokens)}):")
        for t in active_tokens:
            self.stdout.write(
                f"  - {t.prefix}…{t.last_4}  name={t.name!r}  tier={t.max_visibility}  "
                f"last_used={t.last_used_at}"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes made."))
            return

        revoked_keys = api_keys.revoke_all_for_user(user)
        revoked_tokens = url_tokens.revoke_all_for_user(user)
        self.stdout.write(
            self.style.SUCCESS(
                f"Revoked {revoked_keys} key(s) and {revoked_tokens} connector "
                f"URL(s) for {user.email}. Effective now — the cached Principal "
                "for each key was busted inline, and URL tokens are read from "
                "the row on every call. Any client still using one fails on its "
                "next request; the claude.ai connector will need a fresh URL."
            )
        )
