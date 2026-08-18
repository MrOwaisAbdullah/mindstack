"""Input rules for the mint form.

Separated from `generate.py` so the ops view can validate before it
writes, and so the rules are testable without a database — the same
split `apps.mind.consumers` uses for API keys.

The tier check is the one that earns its keep: `max_visibility` reaches
`tiers.tier_for_credential` unmediated, and an unrecognised value there
does not raise — it falls through `TIER_ORDER.get(tier, 0)` to *public*.
A typo would mint a token with less access than the operator asked for
and then render the typo back as though it were real.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.utils import timezone

from apps.url_mcp_tokens.models import URLMCPToken

#: A limit of 0 is a credential that exists and can never be used —
#: revoke says that better. The ceiling is a fat-finger guard, not a
#: capacity claim.
MIN_RATE_LIMIT = 1
MAX_RATE_LIMIT = 10_000

TIERS = [v for v, _ in URLMCPToken.VISIBILITIES]

#: Rendered beside the tier picker. This URL is a bearer secret in a
#: hosted client's config; the choice deserves prose that says so.
TIER_HELP = {
    "public": "Only notes marked visibility: public. Safe for anything you would publish.",
    "agents-only": "Public notes plus agents-only ones. The normal tier for claude.ai.",
    "private": "Everything, including private notes. The whole mind, reachable from a URL.",
}

#: The mint picker. `0` means no expiry, offered last and never default:
#: a credential pasted into a hosted client and forgotten is exactly the
#: one that should age out on its own.
TTL_CHOICES = [7, 30, 90, 180, 365, 0]


class InvalidURLToken(ValueError):
    """Operator-fixable input. The view renders the message as a flash."""


def clean_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise InvalidURLToken(
            "A name is required — it is how you tell connectors apart when revoking one."
        )
    return name[:120]


def clean_tier(raw: str) -> str:
    tier = (raw or "").strip()
    if tier not in TIERS:
        raise InvalidURLToken(f"Unknown tier {raw!r}. Pick one of: {', '.join(TIERS)}.")
    return tier


def clean_rate_limit(raw: str | int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise InvalidURLToken(
            f"Rate limit must be a whole number, got {raw!r}."
        ) from None
    if not MIN_RATE_LIMIT <= value <= MAX_RATE_LIMIT:
        raise InvalidURLToken(
            f"Rate limit must be between {MIN_RATE_LIMIT} and {MAX_RATE_LIMIT} "
            f"requests/minute, got {value}. To stop a token entirely, revoke it."
        )
    return value


def default_ttl_days() -> int:
    """`URL_TOKEN_DEFAULT_TTL_DAYS`, floored at 0 (= never)."""
    return max(0, int(getattr(settings, "URL_TOKEN_DEFAULT_TTL_DAYS", 90)))


def clean_ttl_days(raw: str | int) -> int:
    """Days until expiry. `0` = never; anything above ten years is a typo."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise InvalidURLToken(
            f"Expiry must be a whole number of days, got {raw!r}."
        ) from None
    if value < 0 or value > 3650:
        raise InvalidURLToken(
            f"Expiry must be between 0 (never) and 3650 days, got {value}."
        )
    return value


def expires_at_from_ttl(ttl_days: int) -> dt.datetime | None:
    """Absolute expiry for a TTL in days, or None for "never"."""
    if ttl_days <= 0:
        return None
    return timezone.now() + dt.timedelta(days=ttl_days)
