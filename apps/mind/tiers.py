"""Caller-tier resolution — one place, below every serving layer."""
from __future__ import annotations

from apps.mind.models import Consumer

TIER_ORDER = {"public": 0, "agents-only": 1, "private": 2}


def tier_for_credential(credential: object) -> str:
    """APIKey -> its consumer profile's tier; no profile -> public.

    Two credential types reach here now. A `URLMCPToken` carries its tier
    on the row itself — `Consumer` is a OneToOne to `APIKey` and cannot
    describe one — so it is asked first and answers for itself. An
    `APIKey` keeps taking the join.

    Least privilege either way: an unprofiled key can only read published
    content. (Anonymous callers never reach here — rest.py 401s them
    first.)
    """
    if credential is None:
        return "public"
    # Late import: apps.mind is loaded during app registry population and
    # apps.url_mcp_tokens may not be ready at module import time.
    from apps.url_mcp_tokens import api as url_tokens

    url_tier = url_tokens.tier_for(credential)
    if url_tier is not None:
        return url_tier
    try:
        # NB: not getattr(credential, "consumer_profile", ...) — a missing
        # OneToOne raises DoesNotExist from the descriptor, defaults don't
        # apply.
        profile = Consumer.objects.filter(api_key=credential).first()
    except (TypeError, ValueError):  # not an APIKey (e.g. OAuth token)
        return "public"
    if profile is None:
        return "public"
    # Same clamp as `url_mcp_tokens.api.tier_for`, same reason: an
    # unrecognised stored value answers public instead of passing itself
    # through. The model's `choices` only guard forms — a hand-edited row
    # can hold anything, and unclamped it reached `TIER_ORDER[tier]` rank
    # lookups that 500 on every call that credential makes.
    return profile.max_visibility if profile.max_visibility in TIER_ORDER else "public"


def allows(tier: str, visibility: str) -> bool:
    return TIER_ORDER.get(visibility, 2) <= TIER_ORDER.get(tier, 0)
