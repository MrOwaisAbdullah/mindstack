"""A flipped flag is in the cache, not merely absent from it.

Both flag stores invalidated on write and let the next read repopulate.
The read path is read-then-populate with no interlock, so a reader that
had already fetched the OLD row could land its `cache.set` *after* the
writer's `cache.delete` and pin the stale value for the whole TTL:

    reader   get(key) -> miss
    reader   read DB  -> "0"
    writer   write DB -> "1"
    writer   delete(key)
    reader   set(key, "0")        <- stale, for the full TTL

Five minutes of maintenance mode reading as "off" after the operator
turned it on; thirty seconds of a "this is broken — keep it off"
takedown not taking.

Writing the new value through does not close that race — the reader's
`set` can still land last — and no interlock is being built here. What
it does is make the ordinary case (no concurrent reader) take effect on
the very next request, and shrink the losing interleaving from "any read
in flight" to "a read whose `set` lands after the write". The TTL stays
the honest upper bound, which is what every surface displaying these
flags already tells the operator.

So the assertion is the mid-flight state — what is in the cache the
instant the write returns — not the final value, which reads correctly
either way via the DB.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.core import endpoint_gating, maintenance, runtime_setting_store

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def _no_db(monkeypatch, module, name):
    """Make the DB read explode, so anything that still answers is
    answering from the cache."""
    def boom(*a, **kw):
        raise AssertionError("the DB was consulted — the write did not go through")

    monkeypatch.setattr(module, name, boom)


class TestRuntimeSettings:
    """Covers maintenance mode and the admin IP allowlist — both are
    `RuntimeSetting` rows behind this one store."""

    def test_the_new_value_is_in_the_cache_when_the_write_returns(self) -> None:
        runtime_setting_store.set_value("maintenance_enabled", "1")

        assert cache.get("runtime_setting:maintenance_enabled") == "1", (
            "the write only invalidated — a reader still holding the old row "
            "can now re-cache it for the whole TTL"
        )

    def test_the_flip_is_visible_without_touching_the_database(
        self, monkeypatch
    ) -> None:
        runtime_setting_store.set_value("maintenance_enabled", "1")
        _no_db(monkeypatch, runtime_setting_store, "_read_db")

        assert maintenance.is_enabled() is True

    def test_turning_it_back_off_writes_through_too(self, monkeypatch) -> None:
        """The direction that matters more: a stale "on" locks users out."""
        runtime_setting_store.set_value("maintenance_enabled", "1")
        runtime_setting_store.set_value("maintenance_enabled", "0")
        _no_db(monkeypatch, runtime_setting_store, "_read_db")

        assert maintenance.is_enabled() is False

    def test_a_failed_write_through_drops_the_key_rather_than_keeping_it(
        self, monkeypatch
    ) -> None:
        """An unset key costs a DB read. A stale one is wrong."""
        runtime_setting_store.set_value("maintenance_enabled", "1")

        def failing_set(*a, **kw):
            raise RuntimeError("cache down")

        monkeypatch.setattr(cache, "set", failing_set)
        runtime_setting_store.set_value("maintenance_enabled", "0")

        assert cache.get("runtime_setting:maintenance_enabled") is None


class TestEndpointFlags:
    def test_a_takedown_is_in_the_cache_when_the_write_returns(self) -> None:
        endpoint_gating.set_disabled("ping", True, reason="broken")

        assert cache.get("endpoint_gating:ping") == "1"

    def test_the_takedown_is_visible_without_touching_the_database(
        self, monkeypatch
    ) -> None:
        """Counted rather than raised: `is_disabled` swallows exceptions
        from the DB read by design, so a booby-trap would be reported as
        a wrong return value instead of as the extra query it is."""
        endpoint_gating.set_disabled("ping", True, reason="broken")

        from apps.core.models import EndpointFlag

        queries: list[int] = []
        original = EndpointFlag.objects.filter

        def counting_filter(*a, **kw):
            queries.append(1)
            return original(*a, **kw)

        monkeypatch.setattr(EndpointFlag.objects, "filter", counting_filter)

        assert endpoint_gating.is_disabled("ping") is True
        assert queries == [], "the gate fell through to the DB after a toggle"

    def test_re_enabling_writes_through_too(self) -> None:
        endpoint_gating.set_disabled("ping", True)
        endpoint_gating.set_disabled("ping", False)

        assert cache.get("endpoint_gating:ping") == "0"

    def test_the_database_is_still_the_source_of_truth(self) -> None:
        """Write-through must not become write-only: the row has to be
        there for the next cache miss, and for a Redis wipe."""
        endpoint_gating.set_disabled("ping", True, reason="broken")
        cache.clear()

        assert endpoint_gating.is_disabled("ping") is True
