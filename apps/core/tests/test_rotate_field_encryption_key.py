"""Key rotation must rotate the secrets this project actually stores.

The command walked `django_cryptography` columns, of which there are none
here — the only encrypted data is `AppSetting.value_encrypted`, Fernet
tokens from `apps.brainconfig.crypto`. So it reported "Nothing to do" and
exited 0, and an operator following its own documented workflow would then
swap FIELD_ENCRYPTION_KEY and redeploy. Every read would raise
InvalidToken, which `AppSetting.value` swallows into "" — so the Anthropic
key and webhook secrets silently read as unset with no error anywhere.
"""
from __future__ import annotations

from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.brainconfig.models import AppSetting

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()


def _row(key: str, plaintext: str, fernet_key: str) -> AppSetting:
    token = Fernet(fernet_key.encode()).encrypt(plaintext.encode()).decode("ascii")
    return AppSetting.objects.create(key=key, value_encrypted=token)


def _rotate(**kw):
    out = StringIO()
    call_command("rotate_field_encryption_key", old=OLD_KEY, new=NEW_KEY, stdout=out, **kw)
    return out.getvalue()


@pytest.mark.django_db
class TestRotation:
    def test_re_encrypts_values_so_the_new_key_can_read_them(self):
        _row("ANTHROPIC_API_KEY", "sk-ant-secret", OLD_KEY)
        _rotate()

        token = AppSetting.objects.get(key="ANTHROPIC_API_KEY").value_encrypted
        assert Fernet(NEW_KEY.encode()).decrypt(token.encode()).decode() == "sk-ant-secret"

    def test_reports_what_it_touched(self):
        _row("ANTHROPIC_API_KEY", "a", OLD_KEY)
        _row("WEBHOOK_SECRET", "b", OLD_KEY)
        assert "Re-encrypted: 2 setting(s)" in _rotate()

    def test_dry_run_changes_nothing(self):
        _row("ANTHROPIC_API_KEY", "a", OLD_KEY)
        before = AppSetting.objects.get(key="ANTHROPIC_API_KEY").value_encrypted

        assert "DRY RUN" in _rotate(dry_run=True)
        assert AppSetting.objects.get(key="ANTHROPIC_API_KEY").value_encrypted == before

    def test_is_idempotent(self):
        """An interrupted run must be safe to repeat."""
        _row("ANTHROPIC_API_KEY", "sk-ant-secret", OLD_KEY)
        _rotate()
        out = _rotate()

        assert "Already on the new key: 1 setting(s)" in out
        token = AppSetting.objects.get(key="ANTHROPIC_API_KEY").value_encrypted
        assert Fernet(NEW_KEY.encode()).decrypt(token.encode()).decode() == "sk-ant-secret"

    def test_cleared_settings_are_skipped(self):
        AppSetting.objects.create(key="CLEARED", value_encrypted="")
        _rotate()
        assert AppSetting.objects.get(key="CLEARED").value_encrypted == ""

    def test_unreadable_row_is_left_alone_and_fails_loudly(self):
        """Overwriting it would destroy the only copy."""
        third_key = Fernet.generate_key().decode()
        _row("MYSTERY", "written with a third key", third_key)
        before = AppSetting.objects.get(key="MYSTERY").value_encrypted

        with pytest.raises(CommandError, match="could not be decrypted"):
            _rotate()

        assert AppSetting.objects.get(key="MYSTERY").value_encrypted == before

    def test_a_good_row_beside_a_bad_one_is_still_reported(self):
        _row("GOOD", "value", OLD_KEY)
        _row("MYSTERY", "value", Fernet.generate_key().decode())

        with pytest.raises(CommandError):
            _rotate()


@pytest.mark.django_db
class TestArgumentHandling:
    def test_requires_both_keys(self):
        with pytest.raises(CommandError, match="Both keys are required"):
            call_command("rotate_field_encryption_key", stdout=StringIO())

    def test_refuses_identical_keys(self):
        with pytest.raises(CommandError, match="same key"):
            call_command(
                "rotate_field_encryption_key", old=OLD_KEY, new=OLD_KEY, stdout=StringIO()
            )

    def test_rejects_a_malformed_key_before_touching_any_row(self):
        _row("ANTHROPIC_API_KEY", "a", OLD_KEY)
        before = AppSetting.objects.get(key="ANTHROPIC_API_KEY").value_encrypted

        with pytest.raises(CommandError, match="[Nn]ot a valid Fernet key"):
            call_command(
                "rotate_field_encryption_key", old=OLD_KEY, new="not-a-key", stdout=StringIO()
            )
        assert AppSetting.objects.get(key="ANTHROPIC_API_KEY").value_encrypted == before

    def test_keys_can_come_from_the_environment(self, monkeypatch):
        """Preferred over argv, which is visible in the process list."""
        monkeypatch.setenv("OLD_FIELD_ENCRYPTION_KEY", OLD_KEY)
        monkeypatch.setenv("NEW_FIELD_ENCRYPTION_KEY", NEW_KEY)
        _row("ANTHROPIC_API_KEY", "sk-ant-secret", OLD_KEY)

        call_command("rotate_field_encryption_key", stdout=StringIO())

        token = AppSetting.objects.get(key="ANTHROPIC_API_KEY").value_encrypted
        assert Fernet(NEW_KEY.encode()).decrypt(token.encode()).decode() == "sk-ant-secret"
