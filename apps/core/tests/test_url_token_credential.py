"""The `mcpurl_` credential: registration, shape, liveness, attribution.

DB-free on purpose (CLAUDE.md: the host venv has no `django_redis`, so
`django_db` cannot stand up a cache backend here). That constraint is
useful rather than merely tolerated — pytest-django blocks DB access in
an unmarked test, so "this path does no query" is a property these tests
can actually assert instead of assume.

What is pinned here is what a live run cannot easily show:

- **Both bearer registrations exist.** Missing the rehydrator is silent:
  the caller resolves to no credential, `tier_for_credential` reads that
  as unprofiled, and every MCP call lands on `public` with no error
  anywhere. `docs/DOCS-LAB-FINDINGS.md` §1 is that bug on API keys.
- **Three modules agree on the prefix.** `generate` stores it, the
  scrubber leaves it, the lockout counts against it. If those drift, the
  operator can no longer take a line out of an access log and find the
  row it belongs to.
- **`_stamp_last_used` ignores foreign credential kinds.** A pk alone is
  ambiguous now, and this install happens to have no APIKey whose pk
  matches a token's — so the live pass cannot exercise the collision at
  all.

The tier and revoke behaviour these guard is verified end to end against
the running stack instead; it needs the DB by construction.
"""
from __future__ import annotations

import secrets

import pytest
from django.conf import settings
from django.test import RequestFactory

from apps.core import bearer
from apps.core.events import EndpointCalled
from apps.core.security.lockout import extract_url_token_prefix
from apps.core.security.log_scrub import URLTokenScrubMiddleware, scrub_url_token
from apps.mind.tiers import TIER_ORDER
from apps.url_mcp_tokens import api as url_tokens
from apps.url_mcp_tokens.services import generate as generate_svc


def _fake_secret() -> str:
    """A token of the real shape, never written anywhere."""
    return f"{generate_svc._TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


# ----- Registration -----------------------------------------------------------


def test_the_resolver_is_registered():
    # Without it `/mcp/k/<token>/` cannot authenticate at all — the proxy
    # calls bearer.resolve, which walks exactly this list.
    assert url_tokens.CREDENTIAL_KIND in bearer.registered_names()


def test_the_rehydrator_is_registered():
    """The load-bearing one, and the easy one to forget.

    The proxy strips the credential and forwards only (user_id, kind, pk).
    With no rehydrator for this kind the MCP subprocess rebuilds nothing
    and every caller silently reads at `public` — it fails closed, which
    is why the same omission went unnoticed for a full debugging session
    on API keys.
    """
    assert url_tokens.CREDENTIAL_KIND in bearer.rehydratable_kinds()


def test_the_registered_kind_matches_what_the_backend_returns():
    """`register_rehydrator(kind)` and `Principal.credential_kind` have to
    be the same string, or the bridge looks up a kind nobody registered."""
    from apps.url_mcp_tokens import auth_backend

    assert auth_backend.CREDENTIAL_KIND == url_tokens.CREDENTIAL_KIND


# ----- The scrub middleware ---------------------------------------------------


def test_the_scrub_middleware_is_installed_before_request_id():
    # It publishes `_scrubbed_path`, which downstream writers read instead
    # of `request.path`. Inside RequestIdMiddleware it would be too late
    # for the first log line of the request.
    mw = list(settings.MIDDLEWARE)
    assert "apps.core.security.log_scrub.URLTokenScrubMiddleware" in mw
    assert mw.index("apps.core.security.log_scrub.URLTokenScrubMiddleware") < mw.index(
        "apps.core.middleware.RequestIdMiddleware"
    )


def test_middleware_publishes_the_plaintext_and_a_scrubbed_path():
    secret = _fake_secret()
    request = RequestFactory().post(f"/mcp/k/{secret}/")
    URLTokenScrubMiddleware(lambda r: None)(request)

    assert request._url_token_plain == secret
    assert secret not in request._scrubbed_path
    assert request._scrubbed_path == f"/mcp/k/{secret[:15]}***/"


def test_middleware_leaves_the_routed_path_alone():
    """Rewriting `request.path` here would 404 the route this protects —
    middleware runs before URL dispatch."""
    secret = _fake_secret()
    request = RequestFactory().post(f"/mcp/k/{secret}/")
    URLTokenScrubMiddleware(lambda r: None)(request)

    assert request.path == f"/mcp/k/{secret}/"


def test_middleware_is_a_no_op_for_ordinary_paths():
    request = RequestFactory().get("/ops/connectors/")
    URLTokenScrubMiddleware(lambda r: None)(request)

    assert request._scrubbed_path == "/ops/connectors/"
    assert not hasattr(request, "_url_token_plain")


# ----- One prefix, three modules ----------------------------------------------


def test_mint_scrub_and_lockout_agree_on_the_prefix():
    """The whole point of keeping 8 chars visible: an access-log line is
    traceable to a row. That only holds while all three agree."""
    secret = _fake_secret()
    body = secret[len(generate_svc._TOKEN_PREFIX) :]

    minted_prefix = f"{generate_svc._TOKEN_PREFIX}{body[:8]}"  # generate.py
    assert extract_url_token_prefix(secret) == minted_prefix
    assert scrub_url_token(f"/mcp/k/{secret}/") == f"/mcp/k/{minted_prefix}***/"


def test_the_scrubber_keeps_nothing_of_the_secret_half():
    secret = _fake_secret()
    scrubbed = scrub_url_token(f"GET /mcp/k/{secret}/ HTTP/1.1")
    assert secret[15:] not in scrubbed
    assert secret not in scrubbed


def test_api_key_prefixes_are_left_alone_by_the_url_token_lockout():
    """Separate scopes: enumerating a URL prefix must not lock out a
    bearer key, and vice versa."""
    assert extract_url_token_prefix("mcpsk_AbCdEfGh0123456789") is None


# ----- Tier + limit hooks -----------------------------------------------------


def test_offered_tiers_are_exactly_the_ones_enforcement_understands():
    """The picker cannot offer a tier the read path treats as public.
    `TIER_ORDER.get(tier, 0)` does not raise on a typo — it downgrades."""
    assert set(url_tokens.TIERS) == set(TIER_ORDER)


def test_every_offered_tier_has_help_text():
    assert set(url_tokens.TIER_HELP) == set(url_tokens.TIERS)
    assert all(url_tokens.TIER_HELP[t].strip() for t in url_tokens.TIERS)


def test_tier_and_rate_limit_hooks_decline_credentials_that_are_not_ours():
    """`apps.mind` asks "is this yours?" through these. Answering for an
    APIKey would take the join away from the code that needs it."""
    assert url_tokens.tier_for(None) is None
    assert url_tokens.tier_for(object()) is None
    assert url_tokens.rate_limit_for(None) is None
    assert url_tokens.rate_limit_for(object()) is None


def test_a_token_answers_with_its_own_tier():
    token = url_tokens.URLMCPToken(max_visibility="private", rate_limit_per_min=42)
    assert url_tokens.tier_for(token) == "private"
    assert url_tokens.rate_limit_for(token) == 42


def test_an_unrecognised_stored_tier_reads_as_public():
    """Least privilege on a value no enum should ever hold."""
    token = url_tokens.URLMCPToken(max_visibility="superuser")
    assert url_tokens.tier_for(token) == "public"


# ----- Validation -------------------------------------------------------------


@pytest.mark.parametrize("tier", ["public", "agents-only", "private"])
def test_valid_tiers_pass_through(tier):
    assert url_tokens.clean_tier(tier) == tier


@pytest.mark.parametrize("bad", ["", "  ", "PRIVATE", "agents_only", "admin", None])
def test_unknown_tier_is_rejected_not_silently_downgraded(bad):
    with pytest.raises(url_tokens.InvalidURLToken):
        url_tokens.clean_tier(bad)


def test_name_is_required():
    with pytest.raises(url_tokens.InvalidURLToken):
        url_tokens.clean_name("   ")


@pytest.mark.parametrize("bad", [-1, 3651, "soon", None])
def test_nonsense_ttls_are_rejected(bad):
    with pytest.raises(url_tokens.InvalidURLToken):
        url_tokens.clean_ttl_days(bad)


def test_zero_ttl_means_never():
    assert url_tokens.clean_ttl_days(0) == 0
    assert url_tokens.expires_at_from_ttl(0) is None
    assert url_tokens.expires_at_from_ttl(30) is not None


def test_the_ttl_picker_offers_the_configured_default():
    """A default the form cannot select is a default nobody gets."""
    assert url_tokens.default_ttl_days() in url_tokens.TTL_CHOICES


@pytest.mark.parametrize("bad", [0, -5, 10_001, "lots"])
def test_rate_limits_outside_the_band_are_rejected(bad):
    with pytest.raises(url_tokens.InvalidURLToken):
        url_tokens.clean_rate_limit(bad)


# ----- Liveness ---------------------------------------------------------------


def _user(*, is_active: bool = True):
    """An unsaved User — the FK descriptor type-checks assignment, so a
    duck-typed stand-in raises. (That same ValueError is what a
    URLMCPToken assigned to `Feed.consumer` would have raised in
    production.) Constructing one touches no database."""
    from django.contrib.auth import get_user_model

    return get_user_model()(is_active=is_active)


def _token(**kwargs):
    token = url_tokens.URLMCPToken(**kwargs)
    token.user = _user()
    return token


def test_a_live_token_is_live():
    assert url_tokens.is_live(_token()) is True


def test_a_revoked_token_is_not_live():
    from django.utils import timezone

    assert url_tokens.is_live(_token(revoked_at=timezone.now())) is False


def test_an_expired_token_is_not_live():
    import datetime as dt

    from django.utils import timezone

    past = timezone.now() - dt.timedelta(seconds=1)
    assert url_tokens.is_live(_token(expires_at=past)) is False


def test_a_token_whose_owner_was_deactivated_is_not_live():
    token = url_tokens.URLMCPToken()
    token.user = _user(is_active=False)
    assert url_tokens.is_live(token) is False


def test_no_token_is_not_live():
    assert url_tokens.is_live(None) is False


def test_a_non_url_token_bearer_never_reaches_the_database():
    """The shape check comes first. An unmarked test cannot query, so this
    passing IS the assertion that no lookup happened."""
    from apps.url_mcp_tokens.auth_backend import authenticate_url_token

    assert authenticate_url_token("") is None
    assert authenticate_url_token("mcpsk_AbCdEfGh0123456789") is None
    assert authenticate_url_token("Bearer nonsense") is None


# ----- Attribution ------------------------------------------------------------


class _RecordingManager:
    """Stands in for `APIKey.objects` and records what it was asked for."""

    def __init__(self) -> None:
        self.filtered: list[dict] = []

    def filter(self, **kwargs):
        self.filtered.append(kwargs)
        return self

    def update(self, **kwargs):
        return 0


@pytest.fixture()
def recording_api_keys(monkeypatch):
    from apps.api_keys.models import APIKey

    manager = _RecordingManager()
    monkeypatch.setattr(APIKey, "objects", manager)
    return manager


def _called(**kwargs) -> EndpointCalled:
    base = dict(
        request_id="r1",
        endpoint_slug="get-identity",
        source="mcp",
        status_code=200,
        latency_ms=1,
        credential_id=7,
    )
    base.update(kwargs)
    return EndpointCalled(**base)


def test_a_url_token_call_does_not_stamp_the_api_key_that_shares_its_pk(
    recording_api_keys,
):
    """The reason `credential_kind` crosses on the event at all.

    `last_used_at` is the field an operator checks before revoking
    something; stamping it from an unrelated credential makes a dormant
    key look busy.
    """
    from apps.api_keys.events import _stamp_last_used

    _stamp_last_used(_called(credential_kind="url_token"))
    assert recording_api_keys.filtered == []


def test_an_api_key_call_still_stamps(recording_api_keys):
    from apps.api_keys.events import _stamp_last_used

    _stamp_last_used(_called(credential_kind="api_key"))
    assert recording_api_keys.filtered == [{"pk": 7}]


def test_an_unstated_kind_is_treated_as_an_api_key(recording_api_keys):
    """Back-compat: every fire site that predates the field was an API
    key, and dropping those stamps would be a silent regression."""
    from apps.api_keys.events import _stamp_last_used

    _stamp_last_used(_called())
    assert recording_api_keys.filtered == [{"pk": 7}]
