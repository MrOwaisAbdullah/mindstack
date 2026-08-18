"""A rate limiter must never be the outage.

`config/cache.py` runs django-redis with `IGNORE_EXCEPTIONS=True`, which
is deliberate: a mid-process Redis crash returns `None` instead of
raising, and `cache.get_or_set` on the hot path degrades to the DB.

`_consume` was the one place that did not hold up its end. `cache.incr`
returns `None` under that setting, and `None > limit` is a TypeError —
so a Redis blip produced a 500 on EVERY request across the entire read
surface, REST and MCP alike, for as long as Redis was unwell, with an
error log blaming whichever endpoint was unlucky.

The fix allows rather than denies: a limiter taking the whole API down
is a far worse outcome than the abuse it exists to prevent. But it says
so — a limiter that cannot count must not pretend it did.
"""
from __future__ import annotations

import logging

import pytest
from django.test import override_settings

from apps.mind import throttle


@pytest.fixture(autouse=True)
def locmem():
    from django.core.cache import cache

    override = override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "throttle-unavailable-tests",
            }
        },
        ANONYMOUS_RATE_LIMIT_PER_MIN=3,
    )
    with override:
        cache.clear()
        throttle._last_degraded_report = 0.0
        yield
        cache.clear()


@pytest.fixture()
def redis_down(monkeypatch):
    """django-redis with IGNORE_EXCEPTIONS=True, mid-outage."""
    monkeypatch.setattr(throttle.cache, "incr", lambda *a, **kw: None)
    monkeypatch.setattr(throttle.cache, "add", lambda *a, **kw: None)


class _User:
    is_authenticated = True
    is_staff = True


class TestTheReadSurfaceStaysUp:
    def test_a_none_from_incr_does_not_raise(self, redis_down):
        """The finding: this used to be `TypeError: '>' not supported
        between instances of 'NoneType' and 'int'`."""
        result = throttle.check(user=None, ip="93.184.216.34")
        assert result.allowed is True

    def test_every_request_is_allowed_while_it_lasts(self, redis_down):
        allowed = [throttle.check(user=None, ip="93.184.216.34").allowed for _ in range(50)]
        assert allowed == [True] * 50

    def test_the_per_key_bucket_survives_too(self, redis_down, monkeypatch):
        monkeypatch.setattr(
            throttle, "_consume", throttle._consume
        )  # keep the real one; just exercise the credential path
        result = throttle._consume("brainrl:1", 60, "per-key limit exceeded")
        assert result.allowed is True

    def test_the_result_is_still_a_well_formed_throttle_result(self, redis_down):
        result = throttle.check(user=None, ip="93.184.216.34")
        assert result.limit_per_min == 3
        assert result.retry_after_s == 0
        assert isinstance(result.remaining, int) and result.remaining >= 0


class TestItSaysSo:
    def test_it_logs_a_warning(self, redis_down, caplog):
        with caplog.at_level(logging.WARNING):
            throttle.check(user=None, ip="93.184.216.34")
        assert any("WITHOUT being metered" in r.getMessage() for r in caplog.records)

    def test_the_warning_is_not_repeated_per_request(self, redis_down, caplog):
        """A limiter that logs once per request during a Redis outage is
        its own denial of service."""
        with caplog.at_level(logging.WARNING):
            for _ in range(50):
                throttle.check(user=None, ip="93.184.216.34")
        assert sum("WITHOUT being metered" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.django_db
    def test_it_records_a_degraded_event(self, redis_down):
        from apps.events.models import Event

        throttle.check(user=None, ip="93.184.216.34")

        ev = Event.objects.filter(type="degraded").first()
        assert ev is not None
        assert ev.details["surface"] == "throttle"


class TestNormalOperationIsUnchanged:
    def test_the_limit_still_applies(self):
        allowed = [throttle.check(user=None, ip="93.184.216.34").allowed for _ in range(5)]
        assert allowed == [True, True, True, False, False]

    def test_the_operator_is_still_unlimited(self):
        assert throttle.check(user=_User(), ip="203.0.113.1").allowed is True

    def test_no_warning_is_logged_when_the_counter_works(self, caplog):
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                throttle.check(user=None, ip="93.184.216.34")
        assert not [r for r in caplog.records if "WITHOUT being metered" in r.getMessage()]

    def test_a_missing_key_still_takes_the_first_request_path(self):
        """`incr` raising ValueError for an absent key is normal, not a
        failure — it must not be confused with the outage branch."""
        result = throttle._consume("brainrl:fresh-key", 10, "x")
        assert result.allowed is True
        assert result.remaining == 9
