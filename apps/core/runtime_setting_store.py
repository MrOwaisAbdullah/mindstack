"""Read/write helper for `apps.core.models.RuntimeSetting`.

The four operator-flippable flags (`maintenance_enabled`,
`maintenance_message`, `billing_mode`, `admin_ip_allowlist`,
`audit_retention_days`) all want the same storage shape:

  1. DB is the source of truth (`RuntimeSetting` row, keyed by name).
  2. Redis caches the read with a short TTL so the hot path stays
     sub-ms most of the time.
  3. A write commits the DB row, fires an audit row at the caller's
     scope, then writes the new value THROUGH the cache — see
     `set_value` for why that is not the same as invalidating it.
  4. On Redis outage we fall through to a DB read; on DB outage we
     fall through to the caller-supplied default. The flag never
     "flips by itself" because the cache lost a key.

The read path is read-then-populate with no interlock, so the TTL is
the honest upper bound on how long a flip can take to be seen
everywhere. That is a deliberate ceiling, not an oversight: the
alternative is a version stamp or a lock on a read that happens on
every request, and every surface that displays one of these flags
already tells the operator the propagation window.

Service modules use the thin API here and continue to expose their
existing public surface (`billing_mode.current()`, `maintenance.is_enabled()`,
etc.) so callers don't change.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional, TypeVar

from django.core.cache import cache

log = logging.getLogger(__name__)

# 5-minute read-through. Matches the cache backend default. Long enough
# that the hot path mostly hits Redis; short enough that a forgotten
# cache invalidation surfaces within minutes, not hours.
_CACHE_TTL_SECONDS = 300

# Cache key namespace — distinct from the pre-refactor keys
# (`billing:mode`, `maintenance:enabled`, ...) so an in-flight upgrade
# doesn't read stale pre-refactor values from the new code path.
_CACHE_KEY_PREFIX = "runtime_setting:"


T = TypeVar("T")


def get_str(key: str, default: str) -> str:
    """Return the setting's value as a string, or `default` if unset.

    Order of consultation: Redis → Postgres → caller default. A
    Redis-down boot or `down --volumes` falls through transparently;
    a DB-down read falls through to the default the same way the
    pre-refactor env-var fallback did.
    """
    cache_key = _CACHE_KEY_PREFIX + key
    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    value = _read_db(key)
    if value is None:
        return default
    try:
        cache.set(cache_key, value, timeout=_CACHE_TTL_SECONDS)
    except Exception:
        # IGNORE_EXCEPTIONS on the cache backend usually swallows this;
        # belt-and-suspenders so a backend without that option still
        # serves the right value.
        log.warning(
            "runtime_setting_store: cache write failed for key=%s", key,
            exc_info=True,
        )
    return value


def get_coerced(key: str, default: T, coerce: Callable[[str], T]) -> T:
    """Same as `get_str` but applies `coerce` to the stored string.

    Coerce failures fall back to `default` so a malformed row (e.g.
    operator types nonsense into the admin form, future schema change)
    can't crash callers."""
    raw = get_str(key, default="")
    if not raw:
        return default
    try:
        return coerce(raw)
    except Exception:
        log.warning(
            "runtime_setting_store: coerce failed for key=%s value=%r",
            key, raw,
            exc_info=True,
        )
        return default


def set_value(
    key: str,
    value: str,
    *,
    actor=None,
) -> None:
    """Persist `value` to Postgres and invalidate the read cache.

    Callers issue their own audit row via `apps.core.audit_hook` so
    the action name + before/after stay caller-controlled. This
    helper is the storage primitive only.

    `actor` is the User model instance whose change this is (for the
    `updated_by` FK). None for system / management-command flips.
    """
    from apps.core.models import RuntimeSetting
    from django.db import transaction

    with transaction.atomic():
        RuntimeSetting.objects.update_or_create(
            key=key,
            defaults={"value": value, "updated_by": actor},
        )
    # WRITE-THROUGH, not invalidate. Deleting the key left the next read
    # to repopulate it, and `get_str` is read-then-populate with no
    # interlock: a reader that had already fetched the OLD row could land
    # its `cache.set` after this delete and pin the stale value for the
    # whole TTL — five minutes of maintenance mode reading as "off" after
    # the operator turned it on. Writing the new value here means the
    # common case propagates immediately, and the losing interleaving is
    # narrowed to a reader whose `set` lands after this one.
    #
    # The residual race is NOT closed, deliberately. Closing it needs a
    # version stamp or a lock on a hot per-request read, and the window
    # is already bounded by the same TTL the UI states out loud. This is
    # the acknowledgement, not a claim of atomicity.
    try:
        cache.set(_CACHE_KEY_PREFIX + key, value, timeout=_CACHE_TTL_SECONDS)
    except Exception:
        log.warning(
            "runtime_setting_store: cache write-through failed for key=%s", key,
            exc_info=True,
        )
        # Never leave the OLD value sitting there because the write-through
        # failed — an unset key costs a DB read, a stale one is wrong.
        try:
            cache.delete(_CACHE_KEY_PREFIX + key)
        except Exception:
            log.warning(
                "runtime_setting_store: cache invalidate failed for key=%s", key,
                exc_info=True,
            )


def clear(key: str) -> None:
    """Test helper — drop both the DB row and the cache entry.

    Production code shouldn't need this; operator UIs flip values via
    `set_value(...)` rather than deleting them."""
    from apps.core.models import RuntimeSetting

    try:
        RuntimeSetting.objects.filter(key=key).delete()
    except Exception:
        # DB-down: nothing to clean.
        pass
    try:
        cache.delete(_CACHE_KEY_PREFIX + key)
    except Exception:
        pass


def _read_db(key: str) -> Optional[str]:
    """Single-row DB lookup. Returns None on miss or on DB failure."""
    try:
        from apps.core.models import RuntimeSetting

        return (
            RuntimeSetting.objects.filter(key=key)
            .values_list("value", flat=True)
            .first()
        )
    except Exception:
        log.warning(
            "runtime_setting_store: DB read failed for key=%s", key,
            exc_info=True,
        )
        return None


__all__ = ["clear", "get_coerced", "get_str", "set_value"]
