"""Consumer profiles — per-key tier + rate limit (PLAN.md §5, grill A4).

A side-table on the vendored APIKey (never a column on the framework
app's model, so vendor refreshes stay clean). Every key without a
profile is PUBLIC tier at the default rate limit — least privilege by
default; tiers are granted explicitly in the ops console.
"""
from __future__ import annotations

from django.db import models

DEFAULT_RATE_LIMIT_PER_MIN = 60


class Consumer(models.Model):
    VISIBILITIES = [
        ("public", "public"),
        ("agents-only", "agents-only"),
        ("private", "private"),
    ]

    api_key = models.OneToOneField(
        "api_keys.APIKey", on_delete=models.CASCADE, related_name="consumer_profile"
    )
    max_visibility = models.CharField(max_length=16, choices=VISIBILITIES, default="public")
    rate_limit_per_min = models.PositiveIntegerField(default=DEFAULT_RATE_LIMIT_PER_MIN)
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"{self.api_key} -> {self.max_visibility} @{self.rate_limit_per_min}/min"
