"""Per-KEY rate limiter (grill A3: the template throttles per-user, and
every consumer here is the same user — one chatty agent would starve the
rest). Fixed one-minute window in cache (Redis in prod; LocMem dev).

Registered into apps.core.throttling from MindConfig.ready(); rest.py
passes `credential=` through (marked divergence).

**The no-credential branch used to return unlimited**, on the reasoning
that only session/UI callers reach it and they are all the operator.
That reasoning had two holes:

- It was fail-OPEN by construction. Any caller that reaches the throttle
  without a credential — because a new view is anonymous, or because a
  call site simply forgets to pass `credential=` — silently gets no limit
  at all. `apps.mcp_proxy.views` did exactly that, so every MCP
  `tools/call` was unthrottled while the 429 handling right below the
  call sat there as dead code.
- It conflates "no API key" with "trusted". Those are the same thing only
  while the app has no public surface. The moment an unauthenticated
  endpoint exists, the unlimited branch is the one the internet reaches.

So the fallback is now keyed on the caller instead of the absence of a
credential: the authenticated operator stays unlimited, and everyone else
gets a per-IP bucket. That per-IP bucket is only meaningful because
`apps.core.security.client_ip` resolves the real caller behind a proxy —
before that, every anonymous caller shared one bucket keyed on the
proxy's address, which is a shared-fate DoS rather than a rate limit.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

from apps.core.throttling import ThrottleResult
from apps.mind.models import DEFAULT_RATE_LIMIT_PER_MIN, Consumer

#: Fallback for callers with no API key and no operator session. Modest
#: on purpose: it is a backstop for paths nobody has explicitly sized,
#: not a considered budget for a public endpoint. An endpoint meant to
#: serve the public should be given its own Consumer key and limit.
DEFAULT_ANONYMOUS_RATE_LIMIT_PER_MIN = 20

_WINDOW_SECONDS = 60
#: Two windows, so a counter created at the end of a window survives long
#: enough to be read by requests still inside it.
_COUNTER_TTL = 2 * _WINDOW_SECONDS

_UNLIMITED = ThrottleResult(
    allowed=True, remaining=10**6, retry_after_s=0, limit_per_min=10**6
)


def _anonymous_limit() -> int:
    return int(
        getattr(
            settings,
            "ANONYMOUS_RATE_LIMIT_PER_MIN",
            DEFAULT_ANONYMOUS_RATE_LIMIT_PER_MIN,
        )
    )


def _is_operator(user) -> bool:
    """Staff with a real session — the ops UI driving chat, sync, etc.

    Deliberately narrow. A merely-authenticated non-staff user is treated
    as anonymous: this product has no such users today, so if one appears
    it is a surface nobody has sized a limit for, and the backstop should
    apply rather than the unlimited path.
    """
    return bool(
        getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False)
    )


#: Reporting the degraded counter has to be throttled itself, and the
#: cache is the one thing we cannot use to do it. Process-local clock.
_DEGRADED_REPORT_INTERVAL = 60.0
_last_degraded_report = 0.0


def _report_counter_unavailable() -> None:
    """Say it out loud, at most once a minute per process."""
    global _last_degraded_report
    now = time.monotonic()
    if now - _last_degraded_report < _DEGRADED_REPORT_INTERVAL:
        return
    _last_degraded_report = now
    log.warning(
        "throttle: the cache counter is unavailable — requests are being "
        "allowed WITHOUT being metered until it recovers"
    )
    try:
        from apps.events.models import emit

        emit("degraded", surface="throttle", reason="cache counter unavailable")
    except Exception:  # pragma: no cover - the log line is the contract
        log.debug("throttle: could not record the degraded event", exc_info=True)


def _consume(key: str, limit: int, reason: str) -> ThrottleResult:
    """Fixed-window counter shared by both buckets."""
    window = int(time.time() // _WINDOW_SECONDS)
    cache_key = f"{key}:{window}"
    try:
        count = cache.incr(cache_key)
    except ValueError:
        # No counter for this window yet — the ordinary first request.
        cache.add(cache_key, 1, timeout=_COUNTER_TTL)
        count = 1

    if count is None:
        # django-redis runs with IGNORE_EXCEPTIONS=True, so a connection
        # blip makes `incr` return None instead of raising. `None > limit`
        # is a TypeError — which became a 500 on EVERY request across the
        # entire read surface, REST and MCP alike, for as long as Redis
        # was unwell, with an error log blaming the endpoint.
        #
        # A rate limiter that takes the API down is a far worse outcome
        # than the abuse it exists to prevent, so this allows. But a
        # limiter that cannot count must not pretend it did: say so in
        # the log and the event stream, and let the operator decide.
        _report_counter_unavailable()
        return ThrottleResult(
            allowed=True, remaining=max(0, limit - 1), retry_after_s=0, limit_per_min=limit
        )

    if count > limit:
        return ThrottleResult(
            allowed=False,
            remaining=0,
            retry_after_s=_WINDOW_SECONDS - int(time.time() % _WINDOW_SECONDS),
            limit_per_min=limit,
            reason=reason,
        )
    return ThrottleResult(
        allowed=True, remaining=max(0, limit - count), retry_after_s=0, limit_per_min=limit
    )


def check(
    *,
    user=None,
    endpoint_slug: str = "",
    ip: str | None = None,
    cost: int = 1,
    credential=None,
    **_: object,
) -> ThrottleResult:
    if credential is not None:
        # URL-path tokens carry their own limit and get their own bucket
        # namespace. Sharing `brainrl:{pk}` with API keys would collide on
        # every pk that exists in both tables — token 3 and key 3 are
        # different credentials and would eat each other's budget.
        from apps.url_mcp_tokens import api as url_tokens

        url_limit = url_tokens.rate_limit_for(credential)
        if url_limit is not None:
            return _consume(
                f"brainrl:urltok:{getattr(credential, 'pk', 'x')}",
                url_limit,
                "per-token limit exceeded",
            )

        try:
            profile = Consumer.objects.filter(api_key=credential).first()
        except (TypeError, ValueError):
            # Not an APIKey: a credential type this limiter has never been
            # taught. `filter(api_key=<other>)` raises at queryset
            # construction, which was a 500 on every call — the very shape
            # commit 42a7682 fixed for URL tokens specifically. Meter the
            # stranger at the default limit in its own namespace (typed,
            # so two credential tables can't collide on pk): an unknown
            # credential must not take the API down, and must not pass
            # unmetered either.
            return _consume(
                f"brainrl:other:{type(credential).__name__}:{getattr(credential, 'pk', 'x')}",
                DEFAULT_RATE_LIMIT_PER_MIN,
                "per-credential limit exceeded",
            )
        limit = profile.rate_limit_per_min if profile else DEFAULT_RATE_LIMIT_PER_MIN
        return _consume(
            f"brainrl:{getattr(credential, 'pk', 'x')}",
            limit,
            "per-key limit exceeded",
        )

    if _is_operator(user):
        return _UNLIMITED

    # Anonymous. Bucket per resolved caller address; callers whose address
    # we could not determine share one bucket, which is the fail-closed
    # direction — an unknown address must not mean an unmetered one.
    return _consume(
        f"brainrl:anon:{ip or 'unknown'}",
        _anonymous_limit(),
        "per-ip limit exceeded",
    )
