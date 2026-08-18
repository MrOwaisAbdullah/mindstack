"""Chat — the ops test bench for the smart reader (M3.3, PLAN.md §3/§8).

A session is pinned to ONE tier at creation (the "tier switcher" is
choosing the tier for a new session): every turn runs the reader agent
over that tier's snapshot, so a public-tier session behaves exactly like
the future public clone would.

PRIMARY data (not rebuildable from the repo) — covered by the DB backup
policy (PLAN.md §10).
"""
from __future__ import annotations

from django.db import models


class ChatSession(models.Model):
    TIERS = [
        ("public", "public"),
        ("agents-only", "agents-only"),
        ("private", "private"),
    ]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    tier = models.CharField(max_length=16, choices=TIERS)
    # First user message, truncated — set once, then stable.
    title = models.CharField(max_length=200, blank=True, default="")
    total_input_tokens = models.BigIntegerField(default=0)
    total_output_tokens = models.BigIntegerField(default=0)
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"chat:{self.pk} [{self.tier}] {self.title[:40]}"


class ChatMessage(models.Model):
    ROLES = [("user", "user"), ("assistant", "assistant")]

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    created_at = models.DateTimeField(auto_now_add=True)
    role = models.CharField(max_length=12, choices=ROLES)
    content = models.TextField()
    # What the agent actually opened (observed Read calls, not
    # self-report): [{entity_id, visibility, stale}] — visibility and
    # staleness resolved at serve time (PLAN.md §3).
    sources = models.JSONField(default=list, blank=True)
    # Stream failures land here honestly instead of a half-empty reply.
    error = models.CharField(max_length=200, blank=True, default="")
    sdk_operation = models.ForeignKey(
        "events.SdkOperation", null=True, blank=True, on_delete=models.SET_NULL, related_name="chat_messages"
    )

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"{self.role}@{self.session_id}: {self.content[:40]}"
