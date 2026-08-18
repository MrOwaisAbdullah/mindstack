"""Re-encrypt every stored secret under a new FIELD_ENCRYPTION_KEY.

This command used to walk `django_cryptography` columns. This project has
none — the only encrypted data is `AppSetting.value_encrypted`, a plain
TextField holding Fernet tokens produced by `apps.brainconfig.crypto`,
which uses FIELD_ENCRYPTION_KEY directly as a Fernet key with no PBKDF2
derivation. So the command found nothing, printed "No encrypted fields
discovered. Nothing to do." and exited 0.

That exit code was the dangerous part: an operator following the workflow
in this docstring would then swap the key and redeploy, and every
AppSetting read would raise InvalidToken — which `AppSetting.value`
deliberately swallows, returning "". The Anthropic key, the webhook
secret and the write PAT would all silently read as unset, with no error
anywhere, recoverable only by restoring the old key.

Operator workflow:

    # 1. Generate a NEW Fernet key.
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # 2. Re-encrypt, passing the keys through the environment so they do
    #    not land in shell history or the process list.
    OLD_FIELD_ENCRYPTION_KEY=<current> NEW_FIELD_ENCRYPTION_KEY=<new> \\
        python manage.py rotate_field_encryption_key

    # 3. Deploy the NEW key as FIELD_ENCRYPTION_KEY and restart.

Run it with the app stopped, or at least with nobody editing settings:
a value written between this command's read and its write would be
re-encrypted from the stale plaintext and the operator's edit lost. Rows
are locked FOR UPDATE to keep that window as small as the DB allows.

Idempotent. A row already readable with the NEW key is left alone, so an
interrupted run is safe to repeat.

`--dry-run` reports what would change without writing.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-encrypt AppSetting secrets from the old FIELD_ENCRYPTION_KEY to a new one."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--old",
            default="",
            help=(
                "Current Fernet key. Prefer OLD_FIELD_ENCRYPTION_KEY in the "
                "environment — an argument is visible in the process list."
            ),
        )
        parser.add_argument(
            "--new",
            default="",
            help="Replacement Fernet key. Prefer NEW_FIELD_ENCRYPTION_KEY in the environment.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report without writing.")

    def handle(self, *args: Any, old: str, new: str, dry_run: bool, **options: Any) -> None:
        from apps.brainconfig.models import AppSetting

        old_key = (os.environ.get("OLD_FIELD_ENCRYPTION_KEY") or old).strip()
        new_key = (os.environ.get("NEW_FIELD_ENCRYPTION_KEY") or new).strip()
        if not old_key or not new_key:
            raise CommandError(
                "Both keys are required. Set OLD_FIELD_ENCRYPTION_KEY and "
                "NEW_FIELD_ENCRYPTION_KEY in the environment (preferred), or "
                "pass --old/--new."
            )
        if old_key == new_key:
            raise CommandError("--old and --new are the same key; nothing to rotate.")

        try:
            old_fernet, new_fernet = Fernet(old_key.encode()), Fernet(new_key.encode())
        except (ValueError, TypeError) as exc:
            raise CommandError(
                f"Not a valid Fernet key ({exc}). Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from None

        rotated: list[str] = []
        already: list[str] = []
        unreadable: list[str] = []

        # One transaction, rows locked: a settings write landing between the
        # read and the write would otherwise be silently reverted to its
        # pre-rotation plaintext.
        with transaction.atomic():
            rows = AppSetting.objects.select_for_update().exclude(value_encrypted="").order_by("key")
            for row in rows:
                token = row.value_encrypted.encode("ascii")
                try:
                    plaintext = old_fernet.decrypt(token)
                except InvalidToken:
                    try:
                        new_fernet.decrypt(token)
                    except InvalidToken:
                        # Readable by neither key: rotated from a third key,
                        # or corrupt. Never overwrite it — that would
                        # destroy the only copy.
                        unreadable.append(row.key)
                    else:
                        already.append(row.key)
                    continue

                rotated.append(row.key)
                if not dry_run:
                    row.value_encrypted = new_fernet.encrypt(plaintext).decode("ascii")
                    row.save(update_fields=["value_encrypted"])

            if dry_run:
                transaction.set_rollback(True)

        verb = "Would re-encrypt" if dry_run else "Re-encrypted"
        self.stdout.write(f"{verb}: {len(rotated)} setting(s)")
        for k in rotated:
            self.stdout.write(f"  ~ {k}")
        if already:
            self.stdout.write(f"Already on the new key: {len(already)} setting(s)")
            for k in already:
                self.stdout.write(f"  = {k}")
        if unreadable:
            self.stdout.write(
                self.style.ERROR(
                    f"Unreadable with either key: {len(unreadable)} setting(s) — LEFT UNTOUCHED"
                )
            )
            for k in unreadable:
                self.stdout.write(f"  ! {k}")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes."))
            return

        if unreadable:
            raise CommandError(
                "Some settings could not be decrypted with either key. They were "
                "left as-is. Do NOT deploy the new key yet — re-set those values "
                "in /ops/settings/ first, or restore the key they were written "
                "with."
            )
        log.info("rotate_field_encryption_key: rotated=%s already=%s", len(rotated), len(already))
        self.stdout.write(
            self.style.SUCCESS(
                "Rotation complete. Deploy FIELD_ENCRYPTION_KEY=<new key> and restart."
            )
        )
