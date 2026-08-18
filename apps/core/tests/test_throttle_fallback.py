"""The throttle's no-credential branch.

It used to return unlimited for anyone without an API key. These tests
pin the two properties that replaced it:

- the operator still gets an unmetered ops UI;
- everyone else is metered, including — especially — a caller the code
  simply forgot to identify.

The per-key branch hits `Consumer.objects`, so it needs a database and is
covered by the existing endpoint tests. Everything here stays DB-free
(CLAUDE.md) by exercising only the no-credential paths, with a LocMem
cache so the counter is real but process-local.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.mind import throttle

@pytest.fixture(autouse=True)
def locmem():
    """A real but process-local counter, reset between tests.

    Applied as a fixture rather than a decorator so the cache can be
    cleared *inside* the override — the host venv has no `django_redis`,
    so touching `cache` outside it raises rather than just being slow.
    A fresh `override_settings` per test keeps it re-entrant.
    """
    from django.core.cache import cache

    override = override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "throttle-fallback-tests",
            }
        },
        ANONYMOUS_RATE_LIMIT_PER_MIN=3,
    )
    with override:
        cache.clear()
        yield
        cache.clear()


class _User:
    def __init__(self, authenticated=True, staff=True):
        self.is_authenticated = authenticated
        self.is_staff = staff


# ---- the operator keeps an unmetered ops UI ------------------------------


def test_staff_session_is_unlimited():
    for _ in range(50):
        result = throttle.check(user=_User(), ip="203.0.113.1")
        assert result.allowed


# ---- everyone else is metered -------------------------------------------


def test_anonymous_caller_is_capped():
    allowed = [throttle.check(user=None, ip="93.184.216.34").allowed for _ in range(5)]
    assert allowed == [True, True, True, False, False]


def test_anonymous_denial_carries_retry_after_and_limit():
    for _ in range(4):
        result = throttle.check(user=None, ip="93.184.216.34")
    assert result.allowed is False
    assert result.limit_per_min == 3
    assert 0 < result.retry_after_s <= 60
    assert result.reason == "per-ip limit exceeded"


def test_anonymous_buckets_are_per_ip():
    """One noisy caller must not exhaust another's budget — the whole
    reason the client-IP resolution had to land first."""
    for _ in range(4):
        throttle.check(user=None, ip="93.184.216.34")
    assert throttle.check(user=None, ip="198.18.0.7").allowed is True


def test_authenticated_non_staff_is_treated_as_anonymous():
    user = _User(authenticated=True, staff=False)
    allowed = [throttle.check(user=user, ip="93.184.216.34").allowed for _ in range(5)]
    assert allowed == [True, True, True, False, False]


def test_unknown_ip_is_metered_not_exempt():
    """A caller whose address we could not resolve must fall into a
    bucket, not out of the limiter."""
    allowed = [throttle.check(user=None, ip=None).allowed for _ in range(5)]
    assert allowed == [True, True, True, False, False]


def test_anonymous_callers_without_an_ip_share_one_bucket():
    for _ in range(4):
        throttle.check(user=None, ip=None)
    assert throttle.check(user=None, ip="").allowed is False


# ---- the shape that caused the MCP bug ----------------------------------


def test_forgetting_to_pass_a_credential_is_now_metered():
    """`apps.mcp_proxy.views` called the throttle without `credential=`
    and silently got the unlimited branch. A call site that forgets must
    degrade to a limit, never to no limit."""
    allowed = [
        throttle.check(user=_User(authenticated=False, staff=False), ip="93.184.216.34").allowed
        for _ in range(5)
    ]
    assert allowed == [True, True, True, False, False]


# ---- a third credential type ---------------------------------------------


class _FutureCredential:
    """Neither an APIKey nor a URLMCPToken — the shape an OAuth token or
    any future credential class arrives in."""

    pk = 7


def test_a_third_credential_type_is_metered_not_a_500():
    """`filter(api_key=<non-APIKey>)` raises ValueError at queryset
    construction — commit 42a7682 fixed exactly this for URL tokens; any
    later credential type re-created the 500-on-every-call. A stranger
    gets the default per-key limit in its own bucket. DB-free: the
    ValueError fires before any query would execute."""
    results = [
        throttle.check(credential=_FutureCredential(), ip="93.184.216.34")
        for _ in range(61)
    ]
    assert results[0].allowed is True
    assert results[0].limit_per_min == 60
    assert results[-1].allowed is False
    assert results[-1].reason == "per-credential limit exceeded"


def test_unknown_credential_buckets_do_not_share_with_api_keys():
    """The stranger's bucket is namespaced by type: a future credential
    with pk 7 must not eat the budget of API key 7 (the same collision
    the URL-token namespace comment warns about)."""

    class _OtherCredential:
        pk = 7

    for _ in range(60):
        throttle.check(credential=_FutureCredential(), ip="93.184.216.34")
    assert throttle.check(credential=_FutureCredential(), ip="93.184.216.34").allowed is False
    assert throttle.check(credential=_OtherCredential(), ip="93.184.216.34").allowed is True
