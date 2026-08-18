"""Mint / rotate / revoke API keys.

`generate(user, name)` returns the new APIKey row paired with the *only*
copy of the plaintext secret the caller will ever see. The caller's UI
must render it once and never store it server-side.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.api_keys.models import APIKey

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

# `mcpsk_` = MCP Secret Key. The 32-byte url-safe body gives ~256 bits of
# entropy, which keeps both online + offline brute-force comfortably out
# of reach. Total length lands at `mcpsk_` + ~43 chars.
_KEY_PREFIX = "mcpsk_"
_KEY_BODY_BYTES = 32


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """Returned by `generate()` — UI shows `secret` exactly once."""

    api_key: APIKey
    secret: str  # full plaintext, e.g. "mcpsk_<43 chars>"


def generate(
    user: "AbstractBaseUser",
    *,
    name: str,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> GeneratedKey:
    from apps.api_keys.events import APIKeyCreated
    from apps.core.events import fire

    body = secrets.token_urlsafe(_KEY_BODY_BYTES)
    secret = f"{_KEY_PREFIX}{body}"
    key_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    # `prefix` is the first 8 chars of the body so the UI can show a
    # recognizable handle (`mcpsk_AbCdEfGh…1234`) without revealing the
    # whole key. Last 4 follows the same pattern as Stripe restricted keys.
    prefix = f"{_KEY_PREFIX}{body[:8]}"
    last_4 = secret[-4:]
    api_key = APIKey.objects.create(
        user=user,
        name=name.strip()[:120] or "API key",
        prefix=prefix,
        key_hash=key_hash,
        last_4=last_4,
        scopes=scopes or [],
        expires_at=expires_at,
    )
    fire(
        APIKeyCreated(
            key_id=api_key.pk,
            user_id=user.pk,
            name=api_key.name,
            prefix=api_key.prefix,
        )
    )
    return GeneratedKey(api_key=api_key, secret=secret)


def revoke(api_key: APIKey) -> APIKey:
    """Mark a key revoked. Idempotent — re-revoking is a no-op."""
    from apps.api_keys.events import APIKeyRevoked
    from apps.core.events import fire

    if api_key.revoked_at is None:
        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=["revoked_at", "updated_at"])
        fire(APIKeyRevoked(key_id=api_key.pk, user_id=api_key.user_id))
    return api_key


def revoke_all_for_user(user: "AbstractBaseUser") -> int:
    """Revoke every non-revoked, non-deleted key for the user.

    Returns the count of keys that flipped state. Account-deletion calls
    this so the deleted user's bearer credentials stop working
    immediately, before the 30-day grace + final purge.
    """
    keys = list(
        APIKey.objects.for_user(user).filter(
            revoked_at__isnull=True, is_deleted=False
        )
    )
    for key in keys:
        revoke(key)
    return len(keys)


def rotate(
    api_key: APIKey,
    *,
    name: str | None = None,
) -> GeneratedKey:
    """Create a fresh key under the same user, then revoke the old one.

    The new key inherits scopes + plan_override + expires_at unless
    explicitly overridden. Done in a single transaction so a partial
    failure doesn't leave the user without working credentials.
    """
    from apps.api_keys.events import APIKeyRotated
    from apps.core.events import fire

    with transaction.atomic():
        new = generate(
            api_key.user,
            name=name or api_key.name,
            scopes=list(api_key.scopes),
            expires_at=api_key.expires_at,
        )
        revoke(api_key)
    fire(
        APIKeyRotated(
            old_key_id=api_key.pk,
            new_key_id=new.api_key.pk,
            user_id=api_key.user_id,
        )
    )
    return new
