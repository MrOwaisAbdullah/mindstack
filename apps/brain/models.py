"""Index models — a rebuildable cache of the brain repo, never the truth.

Entity rows are built from frontmatter + filesystem ONLY (INDEX.md is a
generated view; its prose descriptions are harvested as display text but
never trusted for status/visibility — PLAN.md §3, grill B7). If DB and
repo disagree, the repo wins and a drift event is logged (M1.6).
"""
from __future__ import annotations

from django.db import models


class Entity(models.Model):
    """One row per content entity in the brain repo."""

    KINDS = [
        ("identity", "identity"),
        ("project", "project"),
        ("take", "take"),
        ("story", "story"),
        ("lesson", "lesson"),
        ("fact", "fact"),
        ("catalog", "catalog"),
        ("lens", "lens"),
    ]
    VISIBILITIES = [
        ("public", "public"),
        ("agents-only", "agents-only"),
        ("private", "private"),
    ]

    entity_id = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=16, choices=KINDS)
    path = models.CharField(max_length=300, unique=True)  # repo-relative, POSIX
    title = models.CharField(max_length=300, blank=True, default="")
    description = models.TextField(blank=True, default="")
    # Free text on purpose: project cards use "active (pre-launch)" etc.;
    # knowledge notes use current/superseded. Filtering normalizes.
    status = models.CharField(max_length=64, blank=True, default="current")
    superseded_by = models.CharField(max_length=200, blank=True, default="")
    visibility = models.CharField(max_length=16, choices=VISIBILITIES)
    topics = models.JSONField(default=list, blank=True)
    projects = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=300, blank=True, default="")
    source_url = models.CharField(max_length=500, blank=True, default="")
    date = models.CharField(max_length=10, blank=True, default="")  # YYYY-MM
    last_verified = models.DateField(null=True, blank=True)
    content_hash = models.CharField(max_length=64)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["kind", "status"]),
            models.Index(fields=["visibility"]),
        ]
        verbose_name_plural = "entities"

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"[{self.kind}] {self.entity_id} ({self.visibility})"

    @property
    def is_current(self) -> bool:
        return self.status != "superseded"


class SyncRun(models.Model):
    """One row per index rebuild — the sync/drift audit trail."""

    TRIGGERS = [
        ("bootstrap", "bootstrap"),
        ("manual", "manual"),
        ("webhook", "webhook"),
        ("beat", "beat"),
        ("post-feed", "post-feed"),
    ]

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=16, choices=TRIGGERS, default="manual")
    commit_sha = models.CharField(max_length=64, blank=True, default="")
    added = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    removed = models.PositiveIntegerField(default=0)
    drift_detected = models.BooleanField(default=False)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:  # pragma: no cover - repr only
        state = "ok" if self.ok else "FAILED"
        return f"sync {self.trigger} {self.commit_sha[:8]} {state}"
