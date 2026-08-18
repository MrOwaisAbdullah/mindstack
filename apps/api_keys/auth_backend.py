"""Bearer-token authentication backend (cached).

This isn't a `django.contrib.auth.backends.AuthenticationBackend` — those
plug into Django's session login flow. This is a separate, lightweight
function that `EndpointView` calls on every request that carries a
Bearer token. The output is the unified `Principal` from
`apps.core.principal`, the same shape Phase 4's OAuth backend will
return.

`Principal` resolution is cached behind `apps.core.cache`
with a 60s TTL keyed on `apikey:auth:{key_hash}`. The bust subscribers in
`apps.api_keys.events` listen for `APIKeyRevoked` + `APIKeyRotated` and
delete the matching cache key immediately, so revocation is effectively
instantaneous from the user's perspective even with the TTL.

Why we cache the whole Principal (not just the user/credential id):
keeps the hot path zero-DB. The cached User + APIKey are pickled by
django-redis; on read they're depickled into pristine Python objects
that have all the field values needed for the request — `principal.user.pk`,
`is_authenticated`, etc. — without re-issuing a query. The trade-off is
field-staleness: a User flipped to `is_active=False` keeps authenticating
until the cache key TTLs (60s) or an event fires that busts the entry.
For prod, hooking the user-deactivation flow up to a cache.bust is a
simple Phase 9 follow-up.
"""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.api_keys.models import APIKey
from apps.core.cache import cache_get, cache_set, make_key
from apps.core.principal import Principal
from apps.core.security.lockout import (
    clear_api_key_fail,
    is_token_locked,
    record_api_key_fail,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

_AUTH_CACHE_TTL_S = 60


def authenticate_token(token: str) -> Principal | None:
    """Resolve a bearer token to a Principal, or None if it's invalid.

    Sequence:
      1. Cheap shape check on the prefix — bails before any DB work
         (or any cache lookup) for clearly-bogus tokens.
      2. Cached path: `apikey:auth:{key_hash}` → Principal. Bust on
         APIKeyRevoked / APIKeyRotated via subscribers in
         apps.api_keys.events.
      3. On miss: SHA-256 + indexed lookup, plus revocation + expiry
         guards. The hash is the entire identity surface; we never
         store the plaintext.

    Returns None for: missing/empty/non-bearer-shaped tokens, unknown
    tokens, revoked/expired/deleted keys, deactivated users. None
    results are NOT cached today (negative caching invites enumeration
    attacks; see PLAN.md Phase 9.2 brute-force defense for the bucket
    that throttles repeated lookups instead).
    """
    if not token or not token.startswith("mcpsk_"):
        return None

    # prefix-level lockout gate. Once the `mcpsk_<8>`
    # prefix has accumulated 10 failed lookups in 60s the lockout
    # sentinel is set for 5min; further attempts (even with a valid
    # secret) bail out here so brute-force enumerators get nothing
    # back. The view layer (`apps.core.rest`, `apps.mcp_proxy.views`)
    # also calls `is_token_locked` BEFORE us so it can answer 429
    # with the correct retry-after; this duplicate check covers
    # callers that bypass the view layer (tests, mgmt commands).
    if not is_token_locked(token).allowed:
        return None

    key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cache_key = make_key("apikey", "auth", key_hash)

    cached = cache_get(cache_key)
    if isinstance(cached, Principal):
        clear_api_key_fail(token)
        return cached

    # Miss path: hit the DB. We deliberately don't use `cache_get_or_set`
    # here because we need to skip caching for unknown / revoked / expired
    # tokens — negative caching invites enumeration attacks. Phase 9.2's
    # brute-force defense is the bucket that throttles repeated misses.
    api_key = (
        APIKey.objects.select_related("user")
        .filter(key_hash=key_hash)
        .first()
    )

    def _fail() -> None:
        record_api_key_fail(token)

    if api_key is None:
        _fail()
        return None
    if api_key.revoked_at is not None or api_key.is_deleted:
        _fail()
        return None
    if api_key.expires_at is not None and api_key.expires_at <= timezone.now():
        _fail()
        return None
    if not api_key.user.is_active:
        _fail()
        return None

    principal = Principal(
        user=api_key.user,
        credential=api_key,
        credential_kind="api_key",
    )
    cache_set(cache_key, principal, ttl=_AUTH_CACHE_TTL_S)
    clear_api_key_fail(token)
    return principal


def bust_auth_cache(key_hash: str) -> None:
    """Drop the cached Principal for one APIKey.

    Called from the subscribers on `APIKeyRevoked` + `APIKeyRotated`
    (apps/api_keys/events.py) so revocation takes effect immediately
    even with the 60s TTL.
    """
    from apps.core.cache import cache_bust

    cache_bust(make_key("apikey", "auth", key_hash))


def record_use(api_key: APIKey, *, request_id: str, ip: str | None) -> None:
    """Stamp last_used_* on a successful authenticated call.

    Phase 8.5 may rewrite this to debounce writes (last_used_at moves on
    every call → write amplification) but for Phase 3 we keep it simple
    and correct.
    """
    APIKey.objects.filter(pk=api_key.pk).update(
        last_used_at=timezone.now(),
        last_used_ip=ip,
        last_used_request_id=request_id[:64],
    )
