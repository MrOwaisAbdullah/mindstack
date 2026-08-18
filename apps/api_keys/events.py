"""Domain events emitted by `apps.api_keys`.

Canonical names:
    api_keys.key.created
    api_keys.key.rotated
    api_keys.key.revoked
    api_keys.key.used

`key.used` is fired from `apps.core.rest.EndpointView` whenever an
authenticated call lands. Phase 8.5 may debounce the resulting
`last_used_at` write, but for now we update on every event.
"""
from __future__ import annotations

import logging
from typing import ClassVar

from apps.core.events import Event, EndpointCalled, subscribe

log = logging.getLogger(__name__)


class APIKeyCreated(Event):
    event_name: ClassVar[str] = "api_keys.key.created"

    key_id: int
    user_id: int
    name: str
    prefix: str


class APIKeyRotated(Event):
    """Old key revoked; new key created. Both ids in one event for audit."""

    event_name: ClassVar[str] = "api_keys.key.rotated"

    old_key_id: int
    new_key_id: int
    user_id: int


class APIKeyRevoked(Event):
    event_name: ClassVar[str] = "api_keys.key.revoked"

    key_id: int
    user_id: int


# ---- Subscribers --------------------------------------------------------------
# Stamp APIKey.last_used_* whenever an EndpointCalled event names an api_key
# credential. Phase 3.3.5 used to do this inline in EndpointView, but that
# pulled apps.api_keys into apps.core. Subscribing here keeps the boundary
# clean — apps.core only emits the abstract event.


@subscribe(EndpointCalled)
def _stamp_last_used(event: EndpointCalled) -> None:
    if event.credential_id is None:
        return
    # `credential_id` is a pk in whichever table `credential_kind` names.
    # A URL-path MCP token with the same pk as one of these keys would
    # otherwise stamp that key as freshly used — the one fact an operator
    # checks before revoking something. Empty kind means a fire site that
    # predates the field, and every one of those was an API key.
    if event.credential_kind not in ("", "api_key"):
        return
    # Late import — keeps test-only callers (which don't always have the DB
    # ready at module import time) on the lazy path.
    from django.utils import timezone

    from apps.api_keys.models import APIKey

    try:
        APIKey.objects.filter(pk=event.credential_id).update(
            last_used_at=timezone.now(),
            last_used_ip=event.ip,
            last_used_request_id=event.request_id[:64],
        )
    except Exception:
        log.exception(
            "api_keys: stamp_last_used failed",
            extra={"key_id": event.credential_id, "request_id": event.request_id},
        )


# bust the cached Principal on revoke / rotate so the change
# is visible immediately, not after the 60s TTL. We fetch `key_hash` off
# the (still-existing, just soft-deleted) APIKey row to derive the cache
# key. A failed lookup is a no-op — the cache will TTL itself out.


@subscribe(APIKeyRevoked)
def _bust_cache_on_revoke(event: "APIKeyRevoked") -> None:
    _bust_cache_for_key_id(event.key_id)


@subscribe(APIKeyRotated)
def _bust_cache_on_rotate(event: "APIKeyRotated") -> None:
    # The OLD key is the one we cached; the NEW key has a fresh hash and
    # no cache entry yet (it'll be populated on first use).
    _bust_cache_for_key_id(event.old_key_id)


def _bust_cache_for_key_id(key_id: int) -> None:
    from apps.api_keys.auth_backend import bust_auth_cache
    from apps.api_keys.models import APIKey

    try:
        row = APIKey.objects.filter(pk=key_id).only("key_hash").first()
    except Exception:
        log.exception("api_keys: cache-bust lookup failed for key_id=%s", key_id)
        return
    if row is not None:
        bust_auth_cache(row.key_hash)


__all__ = ["APIKeyCreated", "APIKeyRevoked", "APIKeyRotated"]
