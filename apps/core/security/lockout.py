"""Brute-force / abuse counters.

One primitive, sharing the cache + Redis infrastructure (so failure
counters degrade gracefully when Redis is down — same contract as the
rate-limit token bucket):

**Failure-driven lockout** (`record_failure` + `is_locked`) — bumps a
   per-(scope, identifier) counter on each failed attempt; once the
   counter crosses a threshold within a window, sets a separate
   "locked" sentinel that survives for a longer cooldown period. Used
for: failed API-key auth (10 fails / 60s → 5min lockout per prefix),
failed URL-token auth (same thresholds), admin-login (5 fails / 5min →
15min lockout per IP), and honeypot hits.

Two cache keys per (scope, identifier):
  - `lockout:{scope}:{identifier}:count`   (TTL = window_s)
  - `lockout:{scope}:{identifier}:locked`  (TTL = lockout_s; set
    when the counter first crosses the threshold)

Why two keys instead of one bucket: a bucket model couples the
"how fast did fails arrive" question with "how long do we lock
them out for". The fail-count window is short (so a slow attacker
doesn't accumulate a death sentence over weeks) but the lockout
itself needs to be longer than the window so a freshly-locked
attacker can't immediately retry. Two TTLs handle that cleanly.

There was a second primitive here, `consume` — a token-bucket wrapper
that dispatched through a `register_bucket_provider` registry to
`apps.rate_limit`. That app is not vendored, nothing registered a
provider, and the two things it existed for (magic-link issuance, the
OAuth /token endpoint) do not exist in this fork either. It had no
callers and failed open when called. Removed before launch.

`is_token_locked(token)` is a thin public wrapper rest.py + the MCP
proxy call BEFORE bearer resolution: it sniffs an `mcpsk_<8>` prefix
off the bearer token and checks the api-key-fail scope. Locked
tokens get rejected with 429 even when the bearer secret is valid
(the lockout is at the prefix level, not the secret level — the
spec is "even with a valid key").
"""
from __future__ import annotations

import logging
from typing import NamedTuple, Optional

from apps.core.cache import cache_bust, cache_get, cache_set, make_key

log = logging.getLogger(__name__)

# ---- Public dataclass shared by all helpers --------------------------------


class LockoutResult(NamedTuple):
    """Return shape for every check in this module.

    `allowed=False` → caller emits 429 + `Retry-After: retry_after_s`.
    `retry_after_s=0` is the canonical "no wait" value when allowed.
    """

    allowed: bool
    retry_after_s: int


_OK = LockoutResult(allowed=True, retry_after_s=0)


# ---- Failure-driven lockout (counter + sentinel) ---------------------------


def _counter_key(scope: str, identifier: str) -> str:
    return make_key("lockout", scope, identifier, "count")


def _locked_key(scope: str, identifier: str) -> str:
    return make_key("lockout", scope, identifier, "locked")


def is_locked(scope: str, identifier: str) -> LockoutResult:
    """Read-only check — `True` means the (scope, identifier) is currently
    in lockout; the counter could still be bumping but the sentinel
    governs the response.

    Caller pattern:
        result = is_locked("api-key-fail", prefix)
        if not result.allowed:
            return 429 with Retry-After=result.retry_after_s
    """
    if not identifier:
        return _OK
    sentinel = cache_get(_locked_key(scope, identifier))
    if sentinel is None:
        return _OK
    # Best-effort retry-after: we stored the lockout duration alongside
    # the sentinel so the response Retry-After can shrink as time passes.
    # Cache backends don't expose remaining TTL portably, so we store a
    # `(set_at_monotonic, lockout_s)` tuple and compute on read.
    if isinstance(sentinel, tuple) and len(sentinel) == 2:
        import time as _time

        set_at, lockout_s = sentinel
        elapsed = _time.monotonic() - float(set_at)
        remaining = max(1, int(lockout_s - elapsed))
        return LockoutResult(allowed=False, retry_after_s=remaining)
    # Backwards-compat: any non-tuple sentinel just means "locked",
    # default the retry-after to 60s.
    return LockoutResult(allowed=False, retry_after_s=60)


def record_failure(
    scope: str,
    identifier: str,
    *,
    threshold: int,
    window_s: int,
    lockout_s: int,
) -> LockoutResult:
    """Bump the fail counter for (scope, identifier). If the counter
    crosses `threshold` while still inside the `window_s` rolling
    interval, set the locked sentinel for `lockout_s`.

    Returns the post-bump lockout state — callers normally don't need
    it (they're already returning 401/403/whatever for the real auth
    failure), but it's available for tests + audit-log hooks.

    Counter math: we use Django's `cache.set(key, current+1, timeout=window_s)`
    rather than `cache.incr(key)` because:
      - `incr` raises ValueError when the key is missing on some
        backends (LocMemCache); `set` doesn't.
      - We want a fresh `window_s` TTL on each bump so the window is
        rolling, not anchored to first-failure.

    Two cache writes per call (counter + maybe sentinel) is fine —
    failed-auth is the slow path; we're trying to make it slower.
    """
    if not identifier or threshold <= 0:
        return _OK

    counter = _counter_key(scope, identifier)
    locked = _locked_key(scope, identifier)

    current = cache_get(counter) or 0
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = 0
    new_count = current + 1
    # Refresh the window TTL on every bump so a slow attacker can't
    # accumulate fails over hours. If they pause for more than
    # `window_s`, the counter resets to 1.
    cache_set(counter, new_count, ttl=window_s)

    if new_count >= threshold:
        import time as _time

        set_at = _time.monotonic()
        cache_set(locked, (set_at, int(lockout_s)), ttl=lockout_s)
        return LockoutResult(allowed=False, retry_after_s=int(lockout_s))

    return _OK


def clear_failures(scope: str, identifier: str) -> None:
    """Drop the fail counter on success. Does NOT clear the locked
    sentinel — once you're locked, you stay locked for the cooldown
    period even if a single later attempt happens to succeed (which
    shouldn't be possible while the gate is enforcing, but defensive).
    """
    if not identifier:
        return
    cache_bust(_counter_key(scope, identifier))


def force_unlock(scope: str, identifier: str) -> None:
    """Operator escape hatch. Used by the admin panel "Unlock" button
    and by tests that need to reset state. Drops both
    the counter AND the sentinel."""
    if not identifier:
        return
    cache_bust(_counter_key(scope, identifier))
    cache_bust(_locked_key(scope, identifier))


# ---- API-key-prefix lockout helper (used by REST + MCP proxy) --------------


_API_KEY_FAIL_THRESHOLD = 10
_API_KEY_FAIL_WINDOW_S = 60
_API_KEY_FAIL_LOCKOUT_S = 5 * 60  # 5 min

# `mcpsk_` (6) + 8-char body prefix = the dashboard-visible identifier
# stored on `APIKey.prefix`. Lockout is keyed on this slice — every
# secret that shares the prefix shares the lockout.
_PREFIX_LEN = 14
# `mcpurl_` (7) + 8-char body prefix = the dashboard-visible identifier
# stored on `URLMCPToken.prefix`. Same logic as API keys, different
# length because the prefix string itself is one char longer.
_URL_TOKEN_PREFIX_LEN = 15


def extract_api_key_prefix(token: Optional[str]) -> Optional[str]:
    """`mcpsk_AbCdEfGh` slice of the bearer, or None."""
    if not token or not token.startswith("mcpsk_") or len(token) < _PREFIX_LEN:
        return None
    return token[:_PREFIX_LEN]


def extract_url_token_prefix(token: Optional[str]) -> Optional[str]:
    """`mcpurl_AbCdEfGh` slice of a URL-path token, or None.

    Used by `apps.url_mcp_tokens.auth_backend` for the same prefix-level
    brute-force lockout the API-key backend uses for `mcpsk_*` bearers.
    """
    if (
        not token
        or not token.startswith("mcpurl_")
        or len(token) < _URL_TOKEN_PREFIX_LEN
    ):
        return None
    return token[:_URL_TOKEN_PREFIX_LEN]


def is_token_locked(token: Optional[str]) -> LockoutResult:
    """Pre-resolution lockout gate for bearer tokens.

    Caller pattern (from `apps.core.rest::EndpointView` and
    `apps.mcp_proxy.views::mcp_proxy_view`):

        gate = is_token_locked(bearer_token)
        if not gate.allowed:
            return 429 with Retry-After=gate.retry_after_s

    Non-mcpsk_ bearers (OAuth tokens) bypass — OAuth tokens have
    their own throttle on /oauth/token and don't share
    the prefix-shaped enumeration surface.
    """
    prefix = extract_api_key_prefix(token)
    if prefix is None:
        return _OK
    return is_locked("api-key-fail", prefix)


def record_api_key_fail(token: Optional[str]) -> LockoutResult:
    """Record one failed API-key auth attempt against its prefix.

    Called from `apps.api_keys.auth_backend::authenticate_token` when
    a `mcpsk_*` bearer fails to resolve to a `Principal`. Tokens that
    don't carry the `mcpsk_` prefix are no-ops here (they get the
    OAuth throttle path instead).
    """
    prefix = extract_api_key_prefix(token)
    if prefix is None:
        return _OK
    return record_failure(
        "api-key-fail",
        prefix,
        threshold=_API_KEY_FAIL_THRESHOLD,
        window_s=_API_KEY_FAIL_WINDOW_S,
        lockout_s=_API_KEY_FAIL_LOCKOUT_S,
    )


def clear_api_key_fail(token: Optional[str]) -> None:
    """Drop the fail counter for the prefix on a successful auth.
    Doesn't clear the locked sentinel — see `clear_failures`'s docs."""
    prefix = extract_api_key_prefix(token)
    if prefix is None:
        return
    clear_failures("api-key-fail", prefix)


# ---- URL-token-prefix lockout helper (; used by mcp_proxy) -------

# Same thresholds as API-key bearer lockouts. URL tokens have the same
# leakage shape (a stolen token grants account access until rotated) so
# the brute-force defense should be at the same strength.


def is_url_token_locked(token: Optional[str]) -> LockoutResult:
    """Pre-resolution lockout gate for `mcpurl_*` URL-path tokens.

    Mirrors `is_token_locked` but under a different scope key
    (`url-token-fail`) so URL-token enumeration doesn't poison the
    `api-key-fail` bucket and vice-versa. Non-mcpurl_ inputs bypass.
    """
    prefix = extract_url_token_prefix(token)
    if prefix is None:
        return _OK
    return is_locked("url-token-fail", prefix)


def record_url_token_fail(token: Optional[str]) -> LockoutResult:
    """Record one failed URL-token auth attempt against its prefix."""
    prefix = extract_url_token_prefix(token)
    if prefix is None:
        return _OK
    return record_failure(
        "url-token-fail",
        prefix,
        threshold=_API_KEY_FAIL_THRESHOLD,
        window_s=_API_KEY_FAIL_WINDOW_S,
        lockout_s=_API_KEY_FAIL_LOCKOUT_S,
    )


def clear_url_token_fail(token: Optional[str]) -> None:
    """Drop the URL-token fail counter on a successful auth."""
    prefix = extract_url_token_prefix(token)
    if prefix is None:
        return
    clear_failures("url-token-fail", prefix)


__all__ = [
    "LockoutResult",
    "clear_api_key_fail",
    "clear_failures",
    "clear_url_token_fail",
    "extract_api_key_prefix",
    "extract_url_token_prefix",
    "force_unlock",
    "is_locked",
    "is_token_locked",
    "is_url_token_locked",
    "record_api_key_fail",
    "record_failure",
    "record_url_token_fail",
]
