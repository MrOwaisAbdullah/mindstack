"""Cache backend selector with auto-fallback.

Returns the Django `CACHES["default"]` config dict for `base.py` to
consume. Mirrors the pattern from `apps.rate_limit.redis_bucket`:

- Boot-time PING probe (1s timeout). On success → `django-redis`.
- On any failure (REDIS_URL blank, connect refused, auth error, etc.) →
  `LocMemCache`. Logged WARN. The choice is permanent for the process —
  Redis recovery is detected on app restart, not per-request, to avoid
  every miss paying a round-trip penalty.

Runtime safety: `IGNORE_EXCEPTIONS=True` is enabled on the Redis path so
a mid-process Redis crash returns `None` from `cache.get(...)` instead of
raising. Combined with `cache.get_or_set(key, fn, ttl)` everywhere on the
hot path (graceful-degradation contract), DB is the safety
net even if Redis goes down between requests.

Key prefix: `mcp:<env>:v<CACHE_KEY_VERSION>:`. Bumping the version env
var atomically invalidates every key on next deploy — useful when a
schema-changing release would corrupt cache reads from the prior shape.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300  # 5 minutes — overridable by callers via cache.set(..., ttl=...)
_PROBE_TIMEOUT = 1.0


def build_caches(*, redis_url: str, env: str, version: str) -> dict[str, Any]:
    """Return a CACHES dict ready for Django.

    `env` is the deployment environment label ("dev", "prod", "test"...) —
    becomes part of the key prefix so prod and dev never share a key
    namespace even if pointed at the same Redis instance (rare; useful
    for staging-against-prod-redis incidents).

    `version` is the schema-version segment. Bump it on cache-shape-
    breaking deploys.
    """
    key_prefix = f"mcp:{env}:v{version}:"

    if not redis_url:
        log.info("cache: REDIS_URL unset — using LocMemCache (key_prefix=%s)", key_prefix)
        return _locmem(key_prefix)

    if not _redis_reachable(redis_url):
        # _redis_reachable already logged the failure reason.
        return _locmem(key_prefix)

    return {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": redis_url,
            "TIMEOUT": _DEFAULT_TIMEOUT,
            "KEY_PREFIX": key_prefix,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # runtime Redis flakiness becomes None returns,
                # not raised exceptions. Combined with cache.get_or_set
                # this means DB is the safety net.
                "IGNORE_EXCEPTIONS": True,
                # pickle (default) for ORM objects, +
                # client-side compression for >1KB payloads.
                "COMPRESSOR": "django_redis.compressors.zlib.ZlibCompressor",
                # Reasonable connection pool. Bump on prod if /readyz
                # latency on `redis` probe spikes during traffic.
                "CONNECTION_POOL_KWARGS": {"max_connections": 50},
                # Per-call socket timeout — keep this tight so cache
                # ops that hang under network failure don't pile up
                # waiting threads. 1.5s is generous for a LAN Redis.
                "SOCKET_CONNECT_TIMEOUT": 1.5,
                "SOCKET_TIMEOUT": 1.5,
            },
        }
    }


def _locmem(key_prefix: str) -> dict[str, Any]:
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "mcp-default",
            "TIMEOUT": _DEFAULT_TIMEOUT,
            "KEY_PREFIX": key_prefix,
        }
    }


def _redis_reachable(redis_url: str) -> bool:
    """Boot-time PING. Same shape as `apps.rate_limit.redis_bucket`."""
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=_PROBE_TIMEOUT,
            socket_timeout=_PROBE_TIMEOUT,
        )
        client.ping()
        log.info("cache: Redis reachable at %s — using django-redis", redis_url)
        return True
    except Exception as exc:  # noqa: BLE001 — boundary
        log.warning(
            "cache: Redis unreachable at %s (%s) — falling back to LocMemCache",
            redis_url,
            exc,
        )
        return False
