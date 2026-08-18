"""Endpoint runtime enable/disable (10.2.1.11).

Two surfaces:

- :func:`is_disabled` is the hot-path read. Called once per request
  inside :func:`apps.core.rest.make_endpoint_view`. Cached at 30s in
  Redis; falls through to a DB read on miss + on Redis outage.
- :func:`set_disabled` is the operator write. Persists to the DB
  (source of truth) and busts the cache so every worker sees the new
  state on the next request. Emits an audit row via
  :mod:`apps.core.audit_hook` so the toggle shows up in the audit
  timeline.

Design notes:

- DB-backed not Redis-only: operator intent ("this is broken — keep
  it off") MUST survive a Redis incident. Redis-only would silently
  re-enable a buggy endpoint on a cache wipe.
- Cache key namespace ``endpoint_gating:<slug>`` — one key per slug
  so a single Redis GET answers the gate. `set_disabled` writes the
  new state THROUGH the cache rather than invalidating, so a toggle
  normally takes effect on the next request rather than after the
  next miss. The 30s TTL remains the honest upper bound: reads are
  read-then-populate with no interlock, so a reader already holding
  the previous row can still re-cache it after the write. Bounded,
  acknowledged, and not worth a version stamp on a per-request read.
- Cache failure direction: a Redis outage falls through to a DB
  read — slower but correct. Phase 8.6.1 contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.core.cache import cache
from django.utils import timezone

from apps.core import audit_hook

log = logging.getLogger(__name__)

_CACHE_PREFIX = "endpoint_gating:"
_CACHE_TTL_S = 30


@dataclass(frozen=True, slots=True)
class FlagRow:
    """Snapshot of one EndpointFlag row for the admin surface."""

    slug: str
    disabled: bool
    reason: str
    updated_at: object  # datetime; typed loosely so dataclass stays slim
    updated_by_email: str


def _cache_key(slug: str) -> str:
    return f"{_CACHE_PREFIX}{slug}"


def is_disabled(slug: str) -> bool:
    """Hot-path read. True iff the slug has a row with disabled=True.

    Cached at 30s. Cache miss reads the DB. Redis outage falls
    through to a DB read (graceful-degradation).
    """
    if not slug:
        return False
    key = _cache_key(slug)
    try:
        cached = cache.get(key)
        if cached is not None:
            return cached == "1"
    except Exception:
        log.warning("endpoint_gating: cache read failed slug=%s", slug, exc_info=True)
    # Lazy import — this module loads at app start, before the app
    # registry is populated in some import orders.
    from apps.core.models import EndpointFlag

    try:
        disabled = EndpointFlag.objects.filter(slug=slug, disabled=True).exists()
    except Exception:
        log.warning("endpoint_gating: DB read failed slug=%s", slug, exc_info=True)
        return False  # Right failure direction: stay open if DB is unreachable too.
    try:
        cache.set(key, "1" if disabled else "0", timeout=_CACHE_TTL_S)
    except Exception:
        pass
    return disabled


def set_disabled(
    slug: str,
    disabled: bool,
    *,
    reason: str = "",
    actor=None,
    actor_label: str = "",
    request_id: str = "",
    ip: Optional[str] = None,
) -> bool:
    """Flip the flag for `slug` and emit an audit row. Returns True iff
    the persisted state actually changed."""
    if not slug:
        return False
    from apps.core.models import EndpointFlag

    before_disabled = is_disabled(slug)
    before_reason = ""
    try:
        existing = EndpointFlag.objects.filter(slug=slug).first()
        if existing is not None:
            before_reason = existing.reason
    except Exception:
        log.exception("endpoint_gating: DB lookup failed for set_disabled slug=%s", slug)
        return False

    obj, created = EndpointFlag.objects.update_or_create(
        slug=slug,
        defaults={
            "disabled": bool(disabled),
            "reason": (reason or "")[:200],
            "updated_by": actor if actor and getattr(actor, "pk", None) else None,
        },
    )
    # WRITE-THROUGH, not invalidate — see `runtime_setting_store.set_value`
    # for the full note. `is_disabled` is read-then-populate with no
    # interlock, so deleting the key left a reader holding the OLD row
    # free to re-cache it after this write and keep a "this is broken —
    # keep it off" takedown from taking effect for the full 30s. Writing
    # the new state propagates it now; the losing interleaving narrows to
    # a reader whose `set` lands after this one, and stays TTL-bounded.
    try:
        cache.set(_cache_key(slug), "1" if disabled else "0", timeout=_CACHE_TTL_S)
    except Exception:
        # An unset key costs a DB read; a stale one serves a disabled
        # endpoint. Fall back to dropping it.
        try:
            cache.delete(_cache_key(slug))
        except Exception:
            pass

    changed = created or (before_disabled != disabled) or (before_reason != obj.reason)
    if changed:
        audit_hook.record(
            action="settings.endpoint.toggled",
            actor=actor,
            actor_label=actor_label or ("system" if actor is None else ""),
            target_type="endpoint",
            target_id=slug,
            before={"disabled": before_disabled, "reason": before_reason},
            after={"disabled": bool(disabled), "reason": obj.reason},
            ip=ip,
            request_id=request_id,
            is_compliance=False,
        )
    return changed


def list_flags() -> list[FlagRow]:
    """Return every persisted flag, newest-first. Drives the Settings page."""
    from apps.core.models import EndpointFlag

    out: list[FlagRow] = []
    for row in EndpointFlag.objects.select_related("updated_by").order_by("-updated_at"):
        email = row.updated_by.email if row.updated_by_id else ""
        out.append(
            FlagRow(
                slug=row.slug,
                disabled=row.disabled,
                reason=row.reason,
                updated_at=row.updated_at,
                updated_by_email=email,
            )
        )
    return out


def disabled_slugs() -> set[str]:
    """Bulk lookup used by the MCP proxy's `tools/list` filter so one
    listing doesn't fan out a cache.get per tool. One DB query, no cache."""
    from apps.core.models import EndpointFlag

    try:
        return set(
            EndpointFlag.objects.filter(disabled=True).values_list("slug", flat=True)
        )
    except Exception:
        log.warning("endpoint_gating: disabled_slugs DB read failed", exc_info=True)
        return set()


def clear_cache(slug: str = "") -> None:
    """Test helper. Without `slug` clears every flag cache key; with one,
    just that slug's."""
    try:
        if slug:
            cache.delete(_cache_key(slug))
        elif hasattr(cache, "keys"):
            for k in list(cache.keys(f"{_CACHE_PREFIX}*")):
                cache.delete(k)
    except Exception:
        pass


__all__ = [
    "FlagRow",
    "clear_cache",
    "disabled_slugs",
    "is_disabled",
    "list_flags",
    "set_disabled",
]


# Convenience for tests + future references.
def make_disabled_response_payload(slug: str) -> dict[str, object]:
    """Stable shape for the 503 endpoint_disabled body so callers can
    assert against it without hitting the view."""
    return {
        "error": {
            "code": "endpoint_disabled",
            "message": (
                f"The endpoint '{slug}' is temporarily disabled by an "
                f"operator. Try again later."
            ),
        },
        "checked_at": timezone.now().isoformat(),
    }
