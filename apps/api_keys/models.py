"""APIKey model.

Secrets are SHA-256 hashed at rest — the plaintext is shown to the user
exactly once at creation time. Lookup is by `key_hash` (unique), and we
keep `prefix` + `last_4` indexed/visible so the dashboard can render a
recognizable preview without exposing the raw key.

Lifecycle is soft: `revoked_at` blocks new use; `is_deleted` excludes from
list views but keeps the row for audit retention (Phase 9.8 GDPR purge
cron will eventually anonymize). Deleting hard would lose the trail of
who-called-what when investigating an incident.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.permissions import OwnerOnlyManager


class APIKey(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_keys",
    )
    name = models.CharField(max_length=120)
    # `mcpsk_` prefix lets us cheaply detect key shape on the auth path
    # before doing any DB work. The full prefix stored here is `mcpsk_<8>`.
    prefix = models.CharField(max_length=20, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)  # sha256 hex
    last_4 = models.CharField(max_length=4)
    # Phase 6.1 will read scopes for fine-grained authorization. For Phase 3
    # an empty list = full account scope.
    scopes = models.JSONField(default=list, blank=True)
    # Phase 5 fills this in when a user attaches a key to a non-default plan.
    plan_override = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    last_used_request_id = models.CharField(max_length=64, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # `objects.for_user(user)` enforces owner-only filtering;
    # `objects.get_for_user_or_404(user, pk=...)` is the canonical view-side
    # fetch that 404s rather than leaking another user's row.
    objects = OwnerOnlyManager()

    class Meta:
        db_table = "api_keys_api_key"
        indexes = [
            models.Index(fields=["user", "revoked_at"]),
        ]
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None or self.is_deleted:
            return False
        if self.expires_at is not None:
            from django.utils import timezone

            return self.expires_at > timezone.now()
        return True

    def __str__(self) -> str:
        return f"{self.prefix}…{self.last_4} ({self.name})"
