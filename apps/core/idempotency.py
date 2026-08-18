"""Idempotency-Key processing.

Stripe-style replay-safety primitive. The `process_request` helper is
called from `EndpointView` BEFORE running the endpoint; it reads the
`Idempotency-Key` header, looks up (or inserts) an `IdempotentRequest`
row, and returns one of:

  - `Replay(response_status, response_body, response_content_type)` —
    cache hit; EndpointView returns this verbatim. Credits are NOT
    re-charged because the endpoint isn't re-executed.
  - `Mismatch` — same key, different body. EndpointView returns 422
    `idempotency_key_mismatch`.
  - `InFlight` — same key, identical body, but the previous request
    hasn't completed yet (no response_status on the row). Returns
    409 `idempotency_request_in_flight` so the caller knows to wait
    + retry.
  - `Pending(record)` — new key, new row. EndpointView executes the
    endpoint, then calls `cache_response(record, status, body, ct)`
    to fill in the row.
  - `Skipped` — no header, anonymous request, or invalid key. The
    endpoint runs without idempotency processing.

Anonymous requests skip processing entirely — without a user scope,
two random callers could collide on a popular key like `"test"` and
one would get the other's response. Stripe's keys are similarly
scoped under each API key holder.

5xx server errors are NOT cached — operators need to retry transient
failures with the same key. Only 2xx + 4xx land in the cache.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import IdempotentRequest

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

log = logging.getLogger(__name__)

# Stripe's policy: keys are valid for 24h. After that, the cached
# response is gone + a fresh request executes. The daily Q2 cron
# (`purge_expired`) actually deletes the rows so the table doesn't
# grow without bound; the TTL window above is the contract.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# Key length cap (Stripe's policy). Longer keys → 400.
MAX_KEY_LENGTH = 255


# ---- result types -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Replay:
    """Cache hit — EndpointView returns the stored response verbatim."""

    response_status: int
    response_body: bytes
    response_content_type: str


@dataclass(frozen=True, slots=True)
class Mismatch:
    """Same key, different body. EndpointView returns 422."""


@dataclass(frozen=True, slots=True)
class InFlight:
    """Same key, identical body, prior request not yet complete.
    EndpointView returns 409."""


@dataclass(frozen=True, slots=True)
class Pending:
    """New key — endpoint should execute. The record is the row to
    update via `cache_response` once execution completes."""

    record: IdempotentRequest


@dataclass(frozen=True, slots=True)
class Skipped:
    """No idempotency processing — endpoint runs normally."""


@dataclass(frozen=True, slots=True)
class InvalidKey:
    """Header present but malformed (length, non-printable). EndpointView
    returns 400."""

    reason: str


Outcome = Replay | Mismatch | InFlight | Pending | Skipped | InvalidKey


# ---- entry point ------------------------------------------------------------


def process_request(
    *,
    key: str,
    user: "AbstractBaseUser | None",
    method: str,
    path: str,
    body: bytes,
) -> Outcome:
    """Look up (or insert) an IdempotentRequest row.

    `user` is None for anonymous requests → returns Skipped. `key` is
    the raw `Idempotency-Key` header value; empty/whitespace also
    returns Skipped. Length > MAX_KEY_LENGTH returns InvalidKey.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return Skipped()

    key = (key or "").strip()
    if not key:
        return Skipped()

    if len(key) > MAX_KEY_LENGTH:
        return InvalidKey(
            reason=f"Idempotency-Key exceeds max length {MAX_KEY_LENGTH}.",
        )

    request_hash = _hash_body(body)
    user_id = user.pk

    # Fast path: try INSERT. UNIQUE on (user_id, key) ensures only one
    # winner if two concurrent identical requests arrive. Wrap in
    # `transaction.atomic()` so the IntegrityError doesn't poison the
    # outer transaction (django ORM contract).
    try:
        with transaction.atomic():
            record = IdempotentRequest.objects.create(
                key=key,
                user_id=user_id,
                request_method=method[:8],
                request_path=path[:512],
                request_hash=request_hash,
            )
        return Pending(record=record)
    except IntegrityError:
        # Lost the race (or this is a normal replay). Fetch the existing row.
        existing = IdempotentRequest.objects.filter(
            user_id=user_id, key=key
        ).first()
        if existing is None:
            # Vanishingly unlikely — TTL purge ran between the conflict
            # and the SELECT. Treat as a fresh request, retry the
            # INSERT path one more time.
            try:
                with transaction.atomic():
                    record = IdempotentRequest.objects.create(
                        key=key,
                        user_id=user_id,
                        request_method=method[:8],
                        request_path=path[:512],
                        request_hash=request_hash,
                    )
                return Pending(record=record)
            except IntegrityError:
                log.warning(
                    "idempotency: double-conflict on key=%s user=%s",
                    key, user_id,
                )
                return InFlight()

        if existing.request_hash != request_hash:
            return Mismatch()

        if not existing.is_complete:
            return InFlight()

        return Replay(
            response_status=int(existing.response_status or 0),
            response_body=bytes(existing.response_body or b""),
            response_content_type=existing.response_content_type or "application/json",
        )


def cache_response(
    record: IdempotentRequest,
    *,
    status: int,
    body: bytes,
    content_type: str = "application/json",
) -> None:
    """Fill in the response on a Pending row. EndpointView calls this
    after the endpoint has produced a response.

    5xx responses are NOT cached — operators retry transient failures
    with the same key + expect a fresh execution. The Pending row is
    DELETEd in that case so the next replay attempt gets a clean
    INSERT path.
    """
    if 500 <= status < 600:
        try:
            record.delete()
        except Exception:
            log.exception(
                "idempotency: failed to delete row after 5xx (key=%s user=%s)",
                record.key, record.user_id,
            )
        return

    record.response_status = status
    record.response_body = body
    record.response_content_type = content_type[:120] or "application/json"
    record.completed_at = timezone.now()
    try:
        record.save(
            update_fields=[
                "response_status",
                "response_body",
                "response_content_type",
                "completed_at",
            ]
        )
    except Exception:
        log.exception(
            "idempotency: failed to cache response (key=%s user=%s)",
            record.key, record.user_id,
        )


def purge_expired(*, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
    """Delete rows older than `ttl_seconds`. Returns the count removed.

    Daily cron entry point (`apps.core.idempotency.purge_expired` is
    Q2-callable) + the manage.py driver wraps this. Uses a single
    bulk DELETE filtered by the indexed `created_at` so the operation
    is cheap even on a multi-million row table.
    """
    cutoff = timezone.now() - timedelta(seconds=ttl_seconds)
    deleted, _ = IdempotentRequest.objects.filter(created_at__lt=cutoff).delete()
    return deleted


# ---- helpers ----------------------------------------------------------------


def _hash_body(body: bytes) -> str:
    """SHA-256 hex of the raw body bytes. 64 chars."""
    return hashlib.sha256(body or b"").hexdigest()


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "InFlight",
    "InvalidKey",
    "MAX_KEY_LENGTH",
    "Mismatch",
    "Outcome",
    "Pending",
    "Replay",
    "Skipped",
    "cache_response",
    "process_request",
    "purge_expired",
]
