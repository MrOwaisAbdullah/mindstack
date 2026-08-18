"""Public surface of `apps.url_mcp_tokens`.

Other apps import only from this module — `apps.mcp_proxy` for
`record_use`, `apps.mind` for the tier and the rate limit, the ops view
for everything else. Nothing outside reaches into `models`, `services`
or `auth_backend` directly.

`tier_for` and `rate_limit_for` are the two enforcement hooks. Both
answer None for a credential that is not one of ours, which is what lets
`apps.mind` ask "is this yours?" without importing the model or
type-testing it from a distance.
"""
from __future__ import annotations

from apps.url_mcp_tokens.auth_backend import (
    CREDENTIAL_KIND,
    authenticate_url_token,
    record_use,
)
from apps.url_mcp_tokens.models import (
    DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN,
    URLMCPToken,
)
from apps.url_mcp_tokens.services.generate import (
    GeneratedURLToken,
    generate,
    revoke,
    revoke_all_for_user,
    rotate,
)
from apps.url_mcp_tokens.services.listing import (
    get_live_token_by_id,
    get_user_token,
    is_live,
    list_for_user,
)
from apps.url_mcp_tokens.services.validate import (
    MAX_RATE_LIMIT,
    MIN_RATE_LIMIT,
    TIER_HELP,
    TIERS,
    TTL_CHOICES,
    InvalidURLToken,
    clean_name,
    clean_rate_limit,
    clean_tier,
    clean_ttl_days,
    default_ttl_days,
    expires_at_from_ttl,
)


def is_url_token(credential: object) -> bool:
    """True when `credential` is one of ours."""
    return isinstance(credential, URLMCPToken)


def tier_for(credential: object) -> str | None:
    """The tier this credential reads at, or None if it isn't ours.

    An unrecognised stored value answers `public` rather than passing
    itself through: `tiers.TIER_ORDER.get(...)` would silently treat
    anything it doesn't know as public anyway, and saying so here keeps
    the fallback in one place instead of two.
    """
    if not isinstance(credential, URLMCPToken):
        return None
    return credential.max_visibility if credential.max_visibility in TIERS else "public"


def rate_limit_for(credential: object) -> int | None:
    """Requests/minute for this credential, or None if it isn't ours."""
    if not isinstance(credential, URLMCPToken):
        return None
    return credential.rate_limit_per_min or DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN


__all__ = [
    "CREDENTIAL_KIND",
    "DEFAULT_URL_TOKEN_RATE_LIMIT_PER_MIN",
    "GeneratedURLToken",
    "InvalidURLToken",
    "MAX_RATE_LIMIT",
    "MIN_RATE_LIMIT",
    "TIERS",
    "TIER_HELP",
    "TTL_CHOICES",
    "URLMCPToken",
    "authenticate_url_token",
    "clean_name",
    "clean_rate_limit",
    "clean_tier",
    "clean_ttl_days",
    "default_ttl_days",
    "expires_at_from_ttl",
    "generate",
    "get_live_token_by_id",
    "get_user_token",
    "is_live",
    "is_url_token",
    "list_for_user",
    "rate_limit_for",
    "record_use",
    "revoke",
    "revoke_all_for_user",
    "rotate",
    "tier_for",
]
