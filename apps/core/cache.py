"""Cache helpers.

Thin wrappers over Django's `django.core.cache` so feature-app code has
one place to:

- Build namespaced keys with a stable `:` separator (`apikey:abc123`,
  `user:42:plan`, etc.).
- Read with `cache_get_or_set(key, fn, ttl)` so DB is the implicit
  fallback when Redis is unreachable (Phase 8.6.1 contract — the
  django-redis backend's `IGNORE_EXCEPTIONS=True` returns None on
  failure, and `get_or_set` then computes + caches under the same
  key). Same behavior on LocMemCache.
- Bust keys via `cache_bust(key)` from event subscribers
  (`@subscribe(APIKeyRevoked)` etc.).

Why a helper module: `from django.core.cache import cache` works from
anywhere, but using the helpers keeps service-layer call sites
inspectable (every cache read/write logs a structured event), and lets
us swap the backend or add a metric layer in one place if profiling
demands it later.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from django.core.cache import cache as _django_cache

log = logging.getLogger(__name__)

T = TypeVar("T")


# Sentinel so `cache_get` callers can distinguish "key absent" from
# "key cached as None". Internal — don't leak through public API.
_MISS = object()


def make_key(*parts: object) -> str:
    """Build a colon-joined cache key. Coerces every part to str.

    Use this everywhere instead of f-strings so a typo in `:` separators
    can't accidentally land two unrelated namespaces in the same bucket.
    The Django backend's `KEY_PREFIX` is added on top.
    """
    return ":".join(str(p) for p in parts)


def cache_get(key: str) -> Any:
    """Fetch a value or `None`. Never raises on backend failure
    (django-redis is configured with `IGNORE_EXCEPTIONS=True`)."""
    return _django_cache.get(key)


def cache_set(key: str, value: Any, ttl: int | None) -> None:
    """Set with explicit TTL. `ttl=None` uses the backend default."""
    _django_cache.set(key, value, timeout=ttl)


def cache_get_or_set(
    key: str,
    factory: Callable[[], T],
    ttl: int | None,
) -> T:
    """Read-through cache. Computes `factory()` on miss, stores, returns.

    Callers MUST treat `factory` as the source of truth — it's invoked
    on every cache miss and on every Redis-down event (because django-
    redis with IGNORE_EXCEPTIONS turns `cache.get(...)` into None).

    Returns the cached or freshly-computed value either way.
    """
    cached = _django_cache.get(key, default=_MISS)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]
    fresh = factory()
    try:
        _django_cache.set(key, fresh, timeout=ttl)
    except Exception:
        # Belt-and-braces — IGNORE_EXCEPTIONS should already swallow
        # backend failures, but a serializer error (unpicklable value)
        # would still raise here. We log + return the fresh value.
        log.exception("cache.set failed for key=%s", key)
    return fresh


def cache_bust(key: str) -> None:
    """Drop a single key. Safe to call from event subscribers."""
    try:
        _django_cache.delete(key)
    except Exception:
        log.exception("cache.delete failed for key=%s", key)


def cache_bust_many(keys: list[str]) -> None:
    """Drop a list of keys in one round-trip when supported."""
    if not keys:
        return
    try:
        _django_cache.delete_many(keys)
    except Exception:
        log.exception("cache.delete_many failed for %d keys", len(keys))


__all__ = [
    "cache_bust",
    "cache_bust_many",
    "cache_get",
    "cache_get_or_set",
    "cache_set",
    "make_key",
]
