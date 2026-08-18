"""Spend the breaker cannot price must at least be visible.

A run that times out, or whose client hangs up, never receives the
`ResultMessage` carrying `total_cost_usd` — so `cost_usd` stayed NULL
and `today_cost_usd()` saw none of it. The daily cap was being checked
against incomplete data with nothing anywhere saying so.

Worse, the ledger row recorded no tokens either, so the usage page
reported a quiet day for work that really consumed the API.

`StreamEvent.event` is the raw Anthropic streaming payload and those DO
carry usage before any result arrives: `message_start` has the turn's
input and cache counts, `message_delta` the running output count. Those
are banked as they arrive.

Cost is deliberately NOT derived from them. Pricing is per-model and
changes, and a made-up dollar figure inside a circuit breaker is worse
than an admitted blind spot — so the gap is reported instead.
"""
from __future__ import annotations

import asyncio

import pytest
from django.utils import timezone

from apps.events.models import SdkOperation
from apps.reader.services import sdk_runner
from apps.reader.services.sdk_runner import _PartialUsage

from .test_sdk_stream_timeout import (  # noqa: F401 - stub_sdk is a fixture
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    _Stream,
    _run,
    stub_sdk,
)

pytestmark = pytest.mark.django_db


def _start(input_tokens=100, cache_read=20, cache_write=5, output=0):
    return StreamEvent(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                    "output_tokens": output,
                }
            },
        }
    )


def _mdelta(output_tokens):
    return StreamEvent({"type": "message_delta", "usage": {"output_tokens": output_tokens}})


async def _drain():
    out = []
    async for item in sdk_runner.stream_agent(
        kind="reader", tier="public", prompt="hi", append_system=""
    ):
        out.append(item)
    return out


class TestPartialUsageAccumulator:
    def test_it_banks_input_and_cache_counts(self):
        p = _PartialUsage()
        p.observe(_start(input_tokens=100, cache_read=20, cache_write=5).event)
        usage = p.as_usage()

        assert usage["input_tokens"] == 100
        assert usage["cache_read_input_tokens"] == 20
        assert usage["cache_creation_input_tokens"] == 5

    def test_output_is_the_latest_delta_not_a_sum(self):
        """`message_delta` output counts are cumulative WITHIN a turn, so
        adding them up would multiply the real number."""
        p = _PartialUsage()
        p.observe(_start().event)
        for n in (5, 40, 120):
            p.observe(_mdelta(n).event)

        assert p.as_usage()["output_tokens"] == 120

    def test_multiple_turns_are_summed(self):
        p = _PartialUsage()
        p.observe(_start(input_tokens=100).event)
        p.observe(_mdelta(50).event)
        p.observe(_start(input_tokens=200).event)
        p.observe(_mdelta(30).event)
        usage = p.as_usage()

        assert usage["input_tokens"] == 300
        assert usage["output_tokens"] == 80

    def test_it_is_empty_when_nothing_was_seen(self):
        assert _PartialUsage().as_usage() == {}

    def test_unrelated_events_are_ignored(self):
        p = _PartialUsage()
        p.observe({"type": "content_block_delta", "delta": {"text": "hi"}})
        p.observe({})
        assert p.as_usage() == {}


class TestATimedOutRunRecordsItsTokens:
    def test_the_ledger_row_carries_real_counts(self, stub_sdk):
        """The finding. Unfixed this row shows no tokens at all."""
        stream = _Stream([_start(input_tokens=100), _mdelta(60)], stall_after=2)
        stub_sdk.query = lambda **kw: stream

        _run(_drain)

        op = SdkOperation.objects.latest("id")
        assert op.error_class == "Timeout"
        assert op.input_tokens == 100
        assert op.output_tokens == 60
        assert op.cache_read_tokens == 20

    def test_the_cost_stays_null_rather_than_invented(self, stub_sdk):
        stream = _Stream([_start(), _mdelta(60)], stall_after=2)
        stub_sdk.query = lambda **kw: stream

        _run(_drain)

        assert SdkOperation.objects.latest("id").cost_usd is None

    def test_a_completed_run_keeps_its_own_totals(self, stub_sdk):
        """A ResultMessage is authoritative — the partials that led up to
        it must not overwrite it."""
        stream = _Stream([_start(input_tokens=999), _mdelta(999), ResultMessage()])
        stub_sdk.query = lambda **kw: stream

        _run(_drain)

        op = SdkOperation.objects.latest("id")
        assert op.input_tokens == 10 and op.output_tokens == 5
        assert float(op.cost_usd) == 0.01


class TestTheGapIsReported:
    def _finished(self, **kw):
        return SdkOperation.objects.create(
            kind="chat",
            prompt_hash="x" * 64,
            finished_at=timezone.now(),
            **kw,
        )

    def test_unpriced_runs_are_counted(self):
        self._finished(ok=False, error_class="Timeout", input_tokens=100, output_tokens=60)
        self._finished(ok=False, error_class="ClientDisconnected", input_tokens=40, output_tokens=10)

        assert sdk_runner.today_unpriced() == {
            "runs": 2,
            "input_tokens": 140,
            "output_tokens": 70,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 210,
        }

    def test_priced_runs_are_not_counted(self):
        self._finished(ok=True, cost_usd=0.02, input_tokens=100, output_tokens=60)

        assert sdk_runner.today_unpriced()["runs"] == 0

    def test_runs_that_burned_nothing_are_not_counted(self):
        """A run that failed before touching the API is not a blind spot."""
        self._finished(ok=False, error_class="NotConfigured")

        assert sdk_runner.today_unpriced()["runs"] == 0

    def test_running_rows_are_not_counted(self):
        SdkOperation.objects.create(kind="chat", prompt_hash="y" * 64, input_tokens=5)

        assert sdk_runner.today_unpriced()["runs"] == 0

    def test_the_gate_warns_without_refusing(self, monkeypatch, caplog):
        import logging

        self._finished(ok=False, error_class="Timeout", input_tokens=100, output_tokens=60)
        monkeypatch.setattr(sdk_runner.config, "anthropic_api_key", lambda: "sk-test")
        monkeypatch.setattr(sdk_runner.config, "daily_cost_cap", lambda: 10)

        with caplog.at_level(logging.WARNING):
            key = sdk_runner._check_gates(exempt_daily_cap=False)

        assert key == "sk-test"
        assert any("could not be priced" in r.getMessage() for r in caplog.records)

    def test_no_warning_when_everything_is_priced(self, monkeypatch, caplog):
        import logging

        self._finished(ok=True, cost_usd=0.02, input_tokens=100)
        monkeypatch.setattr(sdk_runner.config, "anthropic_api_key", lambda: "sk-test")
        monkeypatch.setattr(sdk_runner.config, "daily_cost_cap", lambda: 10)

        with caplog.at_level(logging.WARNING):
            sdk_runner._check_gates(exempt_daily_cap=False)

        assert not [r for r in caplog.records if "could not be priced" in r.getMessage()]

    def test_the_cap_still_trips_on_priced_spend(self, monkeypatch):
        self._finished(ok=True, cost_usd=99, input_tokens=1)
        monkeypatch.setattr(sdk_runner.config, "anthropic_api_key", lambda: "sk-test")
        monkeypatch.setattr(sdk_runner.config, "daily_cost_cap", lambda: 10)

        with pytest.raises(sdk_runner.DailyCapExceeded):
            sdk_runner._check_gates(exempt_daily_cap=False)


class TestTheUsagePageShowsIt:
    def test_the_view_passes_the_gap_through(self):
        from pathlib import Path

        from django.conf import settings as dj

        src = (Path(dj.BASE_DIR) / "apps/events/ops_views.py").read_text(encoding="utf-8")
        assert "today_unpriced" in src
        assert '"unpriced": unpriced' in src

    def test_the_template_renders_it(self):
        from pathlib import Path

        from django.conf import settings as dj

        html = (Path(dj.BASE_DIR) / "templates/ops/logs.html").read_text(encoding="utf-8")
        assert "unpriced.runs" in html
        assert "never priced" in html
