"""URLMCPToken — the credential that rides in `/mcp/k/<token>/`.

A separate model from `api_keys.APIKey` on purpose, and the reasons are
the reasons the URL surface is different in kind:

- **The secret is in the URL**, so it reaches app logs, the reverse-proxy
  access log, and any browser history that ever held the connector
  address. Everything downstream is written to scrub `mcpurl_<8>***`
  (`apps.core.security.log_scrub`); an `mcpsk_` key in that path is
  scrubbed by nothing.
- **It should expire.** A credential you paste into a hosted client and
  forget is one you want to age out on its own, so `expires_at` is filled
  by default (`URL_TOKEN_DEFAULT_TTL_DAYS`) rather than left null the way
  API keys leave it.
- **Revoking it must not cost you your other clients.** While the URL
  resolved ordinary API keys, pulling the claude.ai connector also
  killed Claude Desktop and Claude Code. Separate rows, separate revoke.

The tier lives here rather than in `mind.Consumer` because that model is
a `OneToOneField` to `APIKey` — it cannot describe this credential at
all. `apps.mind.tiers.tier_for_credential` reads `max_visibility` off
whichever of the two it was handed.

Storage mirrors `APIKey`: SHA-256 at rest, plaintext shown exactly once,
`prefix` + `last_4` kept for a recognizable display handle. `prefix` is
`mcpurl_<8 body chars>` — the same 15-char slice the scrubber leaves in
the logs and the lockout counter keys on, so an operator can match a
suspicious log line to a row on this page.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.permissions import OwnerOnlyManager

#: Requests/minute for a freshly minted token. Deliberately lower than
#: the API-key default: this credential sits in a hosted client we do not
#: control, so its blast radius if the URL leaks should be smaller.
DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN = 60


class URLMCPToken(models.Model):
    VISIBILITIES = [
        ("public", "public"),
        ("agents-only", "agents-only"),
        ("private", "private"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="url_mcp_tokens",
    )
    name = models.CharField(max_length=120)
    # `mcpurl_<8>`. Indexed: the ops page renders it, the log scrubber
    # emits it, and incident response starts by pasting it in here.
    prefix = models.CharField(max_length=24, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)  # sha256 hex
    last_4 = models.CharField(max_length=4)
    # The tier this credential reads at. No second row, no join, and no
    # way for the two to drift apart the way an APIKey and its Consumer
    # profile can (see apps/mind/consumers.py's module docstring).
    max_visibility = models.CharField(
        max_length=16, choices=VISIBILITIES, default="public"
    )
    rate_limit_per_min = models.PositiveIntegerField(
        default=DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN
    )
    # Normally set. The mint flow offers "never" but defaults to
    # `URL_TOKEN_DEFAULT_TTL_DAYS`.
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OwnerOnlyManager()

    class Meta:
        db_table = "url_mcp_tokens_token"
        indexes = [
            models.Index(fields=["user", "revoked_at"]),
        ]
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            from django.utils import timezone

            return self.expires_at > timezone.now()
        return True

    @property
    def state(self) -> str:
        """`active` / `revoked` / `expired`.

        The UI needs to say which: "revoked" is something you did,
        "expired" is something that happened to you, and the fix differs.
        """
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None:
            from django.utils import timezone

            if self.expires_at <= timezone.now():
                return "expired"
        return "active"

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"{self.prefix}…{self.last_4} ({self.name})"
