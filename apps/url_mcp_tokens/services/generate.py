"""Mint / revoke / rotate URL-path MCP tokens.

`generate()` returns the row paired with the *only* copy of the
plaintext anyone will ever see; the ops page renders it once, inside the
full connector URL, and never stores it. Only the SHA-256 is kept, so a
refresh cannot bring it back — the same contract `apps.api_keys` holds
for `mcpsk_` keys, and the same reason: what we cannot produce, we
cannot leak.

Unlike API keys there is no soft-delete flag here. A URL token has one
end state that matters (`revoked_at`) and the row is kept for audit —
the prefix in an old access log has to stay resolvable to a name.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.url_mcp_tokens.models import (
    DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN,
    URLMCPToken,
)
from apps.url_mcp_tokens.services.validate import (
    clean_name,
    clean_rate_limit,
    clean_tier,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

# `mcpurl_` marks the credential as URL-borne everywhere it appears: the
# scrub regex, the lockout scope, and the auth backend's cheap shape
# check all key on it. The 32-byte url-safe body is the same ~256 bits
# `mcpsk_` carries — this secret is more exposed, not less, so it does
# not get a shorter one. Total length lands at `mcpurl_` + ~43 chars.
_TOKEN_PREFIX = "mcpurl_"
_TOKEN_BODY_BYTES = 32


@dataclass(frozen=True, slots=True)
class GeneratedURLToken:
    """Returned by `generate()` — the UI shows `secret` exactly once."""

    token: URLMCPToken
    secret: str  # full plaintext, e.g. "mcpurl_<43 chars>"


def generate(
    user: "AbstractBaseUser",
    *,
    name: str,
    max_visibility: str,
    rate_limit_per_min: int = DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN,
    expires_at: datetime | None = None,
) -> GeneratedURLToken:
    """Mint one token. Validates first: the tier reaches enforcement
    unmediated, so a typo here is a silent downgrade."""
    body = secrets.token_urlsafe(_TOKEN_BODY_BYTES)
    secret = f"{_TOKEN_PREFIX}{body}"
    key_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    # `mcpurl_` + the first 8 body chars — byte-for-byte what
    # `log_scrub.scrub_url_token` leaves behind and what
    # `lockout.extract_url_token_prefix` counts failures against. Those
    # three agreeing is what lets an operator take a line out of an
    # access log and find the row it belongs to.
    prefix = f"{_TOKEN_PREFIX}{body[:8]}"
    token = URLMCPToken.objects.create(
        user=user,
        name=clean_name(name),
        prefix=prefix,
        key_hash=key_hash,
        last_4=secret[-4:],
        max_visibility=clean_tier(max_visibility),
        rate_limit_per_min=clean_rate_limit(rate_limit_per_min),
        expires_at=expires_at,
    )
    return GeneratedURLToken(token=token, secret=secret)


def revoke(token: URLMCPToken) -> URLMCPToken:
    """Kill a token. Idempotent — re-revoking is a no-op.

    Effective immediately: `auth_backend.authenticate_url_token` reads
    the row on every call and caches nothing, so there is no TTL to wait
    out and no cache to bust.
    """
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
    return token


def rotate(token: URLMCPToken, *, name: str | None = None) -> GeneratedURLToken:
    """Mint a replacement carrying the same tier, limit and expiry, then
    revoke the original — in one transaction, so a partial failure never
    leaves the connector without a working credential.

    The new URL has to be pasted into claude.ai by hand; nothing about
    rotation reaches the client on its own.
    """
    with transaction.atomic():
        new = generate(
            token.user,
            name=name or token.name,
            max_visibility=token.max_visibility,
            rate_limit_per_min=token.rate_limit_per_min,
            expires_at=token.expires_at,
        )
        revoke(token)
    return new


def revoke_all_for_user(user: "AbstractBaseUser") -> int:
    """Revoke every live token for the user. Returns how many flipped.

    Account deletion calls this so a URL sitting in someone's connector
    config stops working at the same moment their API keys do.
    """
    tokens = list(URLMCPToken.objects.for_user(user).filter(revoked_at__isnull=True))
    for token in tokens:
        revoke(token)
    return len(tokens)
