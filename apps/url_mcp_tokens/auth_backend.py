"""Resolve a `mcpurl_` URL-path token to a `Principal`.

Registered on `apps.core.bearer` from `AppConfig.ready()`, so the proxy's
`/mcp/k/<token>/` branch resolves it through the same `resolve_bearer`
call every other credential goes through.

**Nothing here is cached, deliberately.** `apps.api_keys` caches the
resolved Principal for 60s and busts it on revoke/rotate via event
subscribers; that is worth its complexity for a credential on the hot
REST path. This one is a single-user connector making at most a handful
of MCP calls a minute, and the property that matters more is that
*revoke is instantly true* — the operator pulls the connector because
the URL leaked. One indexed lookup per call buys that with no cache to
get wrong and no window in which a revoked URL still answers.

Failures feed the `url-token-fail` lockout scope (10 fails / 60s on a
prefix → 5 min of 429), which is separate from `api-key-fail` so that
enumeration against a URL prefix cannot lock out a bearer key that
happens to share nothing but a threshold.
"""
from __future__ import annotations

import hashlib
import logging

from apps.core.principal import Principal
from apps.core.security.lockout import (
    clear_url_token_fail,
    is_url_token_locked,
    record_url_token_fail,
)
from apps.url_mcp_tokens.models import URLMCPToken
from apps.url_mcp_tokens.services.listing import is_live

log = logging.getLogger(__name__)

#: `Principal.credential_kind` for this backend. The proxy forwards it on
#: `X-MCP-Credential-Kind` and the bridge feeds it back to
#: `bearer.rehydrate`, so this string must match the key the rehydrator
#: is registered under in `AppConfig.ready()`.
CREDENTIAL_KIND = "url_token"


def authenticate_url_token(token: str) -> Principal | None:
    """Resolve a URL-path token, or None if it is invalid.

    Returns None for: anything not shaped like `mcpurl_*`, a prefix in
    lockout, an unknown hash, and every dead token (revoked, expired,
    deactivated owner). None is never cached — negative caching invites
    enumeration; the lockout counter is what makes repeated misses
    expensive.
    """
    if not token or not token.startswith("mcpurl_"):
        return None

    # Prefix-level gate. The proxy view checks this first so it can answer
    # 429 with a Retry-After; this duplicate covers callers that bypass
    # the view layer (tests, management commands, a future transport).
    if not is_url_token_locked(token).allowed:
        return None

    key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = (
        URLMCPToken.objects.select_related("user")
        .filter(key_hash=key_hash)
        .first()
    )
    if not is_live(row):
        record_url_token_fail(token)
        return None

    clear_url_token_fail(token)
    return Principal(
        user=row.user,
        credential=row,
        credential_kind=CREDENTIAL_KIND,
    )


def record_use(token: URLMCPToken, *, ip: str | None = None) -> None:
    """Stamp `last_used_*` after a successful call.

    Signature is fixed by the proxy's call site
    (`apps/mcp_proxy/views.py`), which passes `ip=` only. A URL token has
    no `last_used_request_id` column: the request id is in the event log
    and the API-key column exists mostly for REST, which this credential
    does not serve in practice.

    `.update()` on the queryset rather than `.save()` — no signals, one
    statement, and it cannot clobber a concurrently-set `revoked_at` the
    way saving a stale in-memory row could.
    """
    from django.utils import timezone

    URLMCPToken.objects.filter(pk=token.pk).update(
        last_used_at=timezone.now(),
        last_used_ip=ip,
    )
