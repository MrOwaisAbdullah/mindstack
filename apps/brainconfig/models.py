"""Encrypted key/value settings store (PLAN.md §3, grill A13).

The vendored template ships a plaintext `RuntimeSetting` store; this app
adds the encrypted variant for values that must not sit readable in a DB
dump (ANTHROPIC_API_KEY, webhook secrets). Every value goes through
Fernet — even the non-secret ones — so there is exactly one code path.

Empty-string values are treated as UNSET everywhere (Coolify lesson):
`services.get()` falls through to the env default rather than returning "".
"""
from __future__ import annotations

from django.conf import settings as dj_settings
from django.db import models

from . import crypto


class AppSetting(models.Model):
    key = models.CharField(max_length=64, unique=True)
    # Fernet token of the value; "" means explicitly cleared.
    value_encrypted = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:  # pragma: no cover - repr only
        return self.key

    @property
    def value(self) -> str:
        if not self.value_encrypted:
            return ""
        try:
            return crypto.decrypt(self.value_encrypted)
        except crypto.InvalidToken:
            # Wrong FIELD_ENCRYPTION_KEY (rotated without re-encrypting).
            # Surface as unset rather than crashing every reader.
            return ""

    @value.setter
    def value(self, plaintext: str) -> None:
        self.value_encrypted = crypto.encrypt(plaintext) if plaintext else ""
