"""Consumer-key orchestration — the write path behind `/ops/consumers/`.

A working credential is **two rows**. `apps.api_keys` mints the secret;
`Consumer` (this app) grants it a tier and a rate limit. Nothing in
`apps.api_keys` knows the second row exists, so anything that moves one
has to move the other, and that is the entire reason this module exists
rather than the view calling `api_keys.api` directly:

  - `api_keys.generate()` alone yields a key that authenticates perfectly
    and can read almost nothing — `tiers.tier_for_credential` maps an
    unprofiled key to `public`. Deliberate (least privilege), but it
    makes "mint a key" a two-step operation and step two is invisible.
  - `api_keys.rotate()` alone silently **downgrades**: the replacement is
    a different `APIKey` row, the old profile stays bolted to the old
    one, and the caller lands on `public` mid-rotation. The failure is a
    404-shaped "that note doesn't exist", which reads like data loss.

Imports reach `apps.api_keys` only through `apps.api_keys.api`, its
declared public surface.
"""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from apps.api_keys import api as api_keys
from apps.events.models import Event, emit
from apps.mind.models import DEFAULT_RATE_LIMIT_PER_MIN, Consumer

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

# A rate limit of 0 would be a key that exists and can never be used —
# revoke is the way to say that, so the floor is 1. The ceiling is not a
# capacity claim, just a guard against a fat-fingered 600000 quietly
# turning the throttle off.
MIN_RATE_LIMIT = 1
MAX_RATE_LIMIT = 10_000

#: Rendered next to the tier picker. The operator is granting an agent
#: read access to their own private notes; the choice deserves prose.
TIER_HELP = {
    "public": "Only notes marked visibility: public. Safe for anything you would publish.",
    "agents-only": "Public notes plus agents-only ones. The normal tier for an assistant you run yourself.",
    "private": "Everything, including private notes. Grant only to a client you fully control.",
}

TIERS = [v for v, _ in Consumer.VISIBILITIES]


class InvalidConsumer(ValueError):
    """Operator-fixable input. The view renders the message as a flash."""


# ---- validation ---------------------------------------------------------------


def clean_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise InvalidConsumer("A name is required — it is how you tell keys apart when revoking one.")
    return name[:120]


def clean_tier(raw: str) -> str:
    tier = (raw or "").strip()
    if tier not in TIERS:
        raise InvalidConsumer(f"Unknown tier {raw!r}. Pick one of: {', '.join(TIERS)}.")
    return tier


def clean_rate_limit(raw: str | int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise InvalidConsumer(f"Rate limit must be a whole number, got {raw!r}.") from None
    if not MIN_RATE_LIMIT <= value <= MAX_RATE_LIMIT:
        raise InvalidConsumer(
            f"Rate limit must be between {MIN_RATE_LIMIT} and {MAX_RATE_LIMIT} "
            f"requests/minute, got {value}. To stop a key entirely, revoke it."
        )
    return value


# ---- write path ---------------------------------------------------------------


@transaction.atomic
def create(
    user: "AbstractBaseUser",
    *,
    name: str,
    max_visibility: str,
    rate_limit_per_min: int,
    note: str = "",
) -> api_keys.GeneratedKey:
    """Mint a key AND its profile. Returns the one-and-only plaintext."""
    generated = api_keys.generate(user, name=clean_name(name))
    Consumer.objects.create(
        api_key=generated.api_key,
        max_visibility=clean_tier(max_visibility),
        rate_limit_per_min=clean_rate_limit(rate_limit_per_min),
        note=(note or "").strip()[:200],
    )
    emit(
        "settings_change",
        consumer=generated.api_key,
        action="consumer_created",
        prefix=generated.api_key.prefix,
        name=generated.api_key.name,
        tier=max_visibility,
    )
    return generated


def update_profile(
    api_key,
    *,
    max_visibility: str,
    rate_limit_per_min: int,
    note: str = "",
) -> Consumer:
    """Change tier / limit / note on an existing key.

    No cache bust needed and none is done: `tiers.tier_for_credential`
    and `throttle.check` both re-read `Consumer` on every request. Only
    the *Principal* is cached (60s), and that carries no tier.
    """
    tier = clean_tier(max_visibility)
    limit = clean_rate_limit(rate_limit_per_min)
    profile, _ = Consumer.objects.update_or_create(
        api_key=api_key,
        defaults={
            "max_visibility": tier,
            "rate_limit_per_min": limit,
            "note": (note or "").strip()[:200],
        },
    )
    emit(
        "settings_change",
        consumer=api_key,
        action="consumer_updated",
        prefix=api_key.prefix,
        tier=tier,
        rate_limit_per_min=limit,
    )
    return profile


@transaction.atomic
def rotate(api_key, *, name: str | None = None) -> api_keys.GeneratedKey:
    """Replace a key, carrying its tier and limit onto the new one.

    Read the module docstring before simplifying this to a bare
    `api_keys.rotate()`: the profile does not follow on its own, and the
    resulting downgrade is silent.
    """
    old = Consumer.objects.filter(api_key=api_key).first()
    generated = api_keys.rotate(api_key, name=name)
    if old is not None:
        Consumer.objects.create(
            api_key=generated.api_key,
            max_visibility=old.max_visibility,
            rate_limit_per_min=old.rate_limit_per_min,
            note=old.note,
        )
    emit(
        "settings_change",
        consumer=generated.api_key,
        action="consumer_rotated",
        old_prefix=api_key.prefix,
        prefix=generated.api_key.prefix,
        tier=old.max_visibility if old else "public",
    )
    return generated


def revoke(api_key) -> None:
    """Kill a key. `api_keys` busts the auth cache, so this is immediate."""
    api_keys.revoke(api_key)
    emit(
        "settings_change",
        consumer=api_key,
        action="consumer_revoked",
        prefix=api_key.prefix,
        name=api_key.name,
    )


# ---- read path ----------------------------------------------------------------


def rows(user: "AbstractBaseUser", *, days: int = 7) -> list[dict]:
    """One display row per key: the credential, its profile, its traffic.

    Unprofiled keys are surfaced explicitly rather than silently rendered
    as public — a key minted from a shell (the only way to make one
    before this page existed) has no profile, and "public" would look
    like a decision somebody made.
    """
    keys = api_keys.list_for_user(user)
    if not keys:
        return []

    by_key = {c.api_key_id: c for c in Consumer.objects.filter(api_key__in=keys)}
    since = timezone.now() - dt.timedelta(days=days)
    reads = dict(
        Event.objects.filter(
            type="read", consumer__in=keys, created_at__gte=since
        )
        .values_list("consumer_id")
        .annotate(n=Count("id"))
    )
    # `APIKey.last_used_at` is stamped from a subscriber on
    # `EndpointCalled`, which both surfaces now fire (REST joined MCP in
    # the pre-launch hardening; before that a key used exclusively over
    # REST read as "never used" — the exact fact an operator checks
    # before revoking something). The event log still gets a say: it is
    # written by the endpoints themselves, so it covers any window where
    # the subscriber failed, and taking whichever is later costs one
    # query.
    #
    # `settings_change` is excluded and that exclusion is load-bearing:
    # this module's own audit rows (created / updated / rotated / revoked)
    # carry `consumer=<the key>`, so counting them would date "last used"
    # to the moment the key was MINTED and no key would ever read as
    # unused. Everything else attributed to a credential is a call it made.
    last_event = dict(
        Event.objects.filter(consumer__in=keys)
        .exclude(type="settings_change")
        .values_list("consumer_id")
        .annotate(t=Max("created_at"))
    )

    out = []
    for key in keys:
        profile = by_key.get(key.pk)
        out.append(
            {
                "key": key,
                "profile": profile,
                "tier": profile.max_visibility if profile else "public",
                "unprofiled": profile is None,
                "rate_limit": profile.rate_limit_per_min if profile else DEFAULT_RATE_LIMIT_PER_MIN,
                "note": profile.note if profile else "",
                "reads": reads.get(key.pk, 0),
                "last_used": _latest(key.last_used_at, last_event.get(key.pk)),
                # `is_active` folds revoked + deleted + expired together;
                # the UI needs to say WHICH, because "revoked" is a thing
                # you did and "expired" is a thing that happened to you.
                "state": _state(key),
            }
        )
    return out


def _latest(*stamps):
    """Newest non-null timestamp, or None if there is none."""
    real = [s for s in stamps if s is not None]
    return max(real) if real else None


def _state(key) -> str:
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None and key.expires_at <= timezone.now():
        return "expired"
    return "active"
