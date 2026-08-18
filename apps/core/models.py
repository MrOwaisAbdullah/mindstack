"""Framework-tier models for `apps.core`.

apps.core is otherwise model-free — it owns the framework primitives
(EndpointView, registry, security helpers, event bus).
The Idempotency-Key surface is the first thing here that needs
durable per-row state (one row per (user, key) pair, 24h TTL), so
this module exists to give it a home without dropping it into a
feature app.

Future framework primitives that need persistence (e.g. a flag store)
can land alongside; everything here MUST stay free of cross-app
imports per import-linter Contract 1.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class IdempotentRequest(models.Model):
    """Stripe-style replay-safety record.

    One row per (user, Idempotency-Key) pair. EndpointView consults
    this table BEFORE executing a request that carries an
    `Idempotency-Key` header:

      - hash matches stored row + response present → return the cached
        (response_status, response_body, response_content_type)
      - hash matches stored row + no response yet → 409 (an identical
        request is currently in flight; the user should retry)
      - hash differs from stored row → 422 idempotency_key_mismatch
      - no row → INSERT (pending) + execute + UPDATE with the response

    UNIQUE on (user_id, key) means user A's keys never collide with
    user B's keys. Anonymous requests SKIP idempotency entirely
    because there's no user scope; the spec's verify gate runs over
    authenticated calls.

    Cached `response_body` is bytes so we can replay the exact
    server-side bytes (including JSON whitespace, byte-encoding
    quirks). `response_content_type` is preserved separately so the
    replay carries the correct Content-Type header.
    """

    # Stripe accepts up to 255-char keys; we mirror that.
    key = models.CharField(max_length=255)
    user_id = models.BigIntegerField(
        help_text="The user this row is scoped under. Anonymous requests "
        "skip idempotency entirely.",
    )
    request_method = models.CharField(max_length=8)
    request_path = models.CharField(max_length=512)
    # SHA-256 hex of the raw request body bytes. 64 chars.
    request_hash = models.CharField(max_length=64)

    # Empty until the endpoint finishes executing — set in the same
    # row UPDATE that marks the request "complete". A second
    # concurrent request that arrives between INSERT and UPDATE sees
    # response_status=NULL and 409s.
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.BinaryField(blank=True, default=b"")
    response_content_type = models.CharField(max_length=120, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_idempotent_request"
        constraints = [
            # One row per (user, key) — UNIQUE means concurrent
            # identical requests race on INSERT; the loser fetches the
            # winner's row.
            models.UniqueConstraint(
                fields=["user_id", "key"],
                name="uniq_idempotent_request_user_key",
            ),
        ]
        indexes = [
            # `created_at` index covers the daily TTL purge.
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        state = "complete" if self.response_status else "pending"
        return f"IdempotentRequest({self.user_id}:{self.key[:12]}…, {state})"

    @property
    def is_complete(self) -> bool:
        return self.response_status is not None


class RuntimeSetting(models.Model):
    """Durable store for operator-flippable runtime flags.

    Phase 10.2.1.11 originally kept these flags in Redis only
    (`maintenance:enabled`, `billing:mode`, `settings:admin_ip_allowlist`,
    `settings:audit_retention_days`). That works while Redis is healthy,
    but a `docker compose down --volumes`, a boot-time Redis probe
    failure, or any unreachable-cache event silently reverts every
    operator-chosen value to its env-var default — and the audit log
    captures the *history* of changes, not the *current* state.

    Same shape as `EndpointFlag`: DB is the source of truth, Redis is
    a 5-minute read-through cache on the hot path. A Redis outage
    causes a slower read (DB round-trip), never the wrong answer.

    `key` is a short slug (e.g. `"billing_mode"`); also the primary
    key — there's at most one row per setting and callers always look
    up by name. `value` is stored as text so any setting (string, int,
    bool serialized as "1"/"0", comma-joined CIDR list) shares one
    table; the service layer does the coerce. `updated_by` is nullable
    on purpose — system flips and management-command seeds have no
    actor, and a user delete cascades to NULL so the setting itself
    survives.
    """

    key = models.CharField(max_length=64, primary_key=True)
    value = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "core_runtime_setting"
        ordering = ["key"]

    def __str__(self) -> str:
        # Truncate long values (CIDR list, JSON blob, etc.) so admin
        # list pages stay readable.
        v = self.value if len(self.value) <= 60 else self.value[:57] + "..."
        return f"RuntimeSetting({self.key!r}={v!r})"


class EndpointFlag(models.Model):
    """Per-endpoint runtime gating flag (10.2.1.11).

    The endpoint registry is import-time only — `@endpoint` registers
    each spec at module load. There's no runtime way to take an
    endpoint offline without a redeploy. This row gives operators that
    knob: `disabled=True` makes `make_endpoint_view` return
    503 endpoint_disabled before the body is parsed.

    The row used to carry a second flag, `admin_only` ("hide from
    non-staff"), inherited from the multi-tenant template. It was
    removed before launch: this is a single-operator product, setup
    sets `is_staff=True` on the one account every credential resolves
    to, so the gate could never fire. It read as a live feature and
    enforced nothing. `disabled` is the surviving knob.

    Storage strategy: DB is the source of truth, Redis caches the
    "is this slug disabled?" lookup on the hot path. Reasoning:

    - Operator intent ("keep this off until I fix the bug") MUST
      survive a Redis incident. Redis-only would silently re-enable
      a buggy endpoint when the cache wipes.
    - The DB read is a single indexed lookup; cached at 30s so the
      hot path mostly hits Redis. Phase 8.6.1 graceful-degradation
      contract holds: Redis down → DB read still works.
    - Same pattern as the Phase 8.2 plan-entitlement + API-key caches.

    Slug is unique because there's at most one flag per endpoint slug
    across the registry. We don't model versioning here (`v2/foo` vs
    `v1/foo`) because the endpoints page lists by slug + the operator
    intent is "this NAME is broken, regardless of version" — if a
    future deployment needs per-version flags, add `version` to the
    UNIQUE.
    """

    slug = models.CharField(max_length=64, unique=True)
    disabled = models.BooleanField(default=False, db_index=True)
    reason = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Free-form note shown to operators on the endpoints list.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "core_endpoint_flag"

    def __str__(self) -> str:
        return f"EndpointFlag({self.slug}, {'disabled' if self.disabled else 'enabled'})"
