"""The blind-spot figure has to count the tokens the workload actually uses.

`today_unpriced()` is the operator's one signal that the daily-cap
breaker is measuring against incomplete data. It summed `input_tokens`
and `output_tokens` only.

On this workload those two are a rounding error. Every run replays a
system prompt and a tier snapshot through the prompt cache, so the money
is in `cache_read_tokens` and `cache_write_tokens`. A real cut-off run,
measured against the live stack, recorded:

    in=2  out=2  cache_read=11689  cache_write=2212

— reported as **4 tokens** out of 13,905. A blind-spot figure that
understates by three orders of magnitude is not an admission, it is the
same silence in smaller type.

A run can also be *entirely* cache reads, in which case the old `> 0`
filter excluded it from the gap altogether — it looked like a run that
never touched the API.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.events.models import SdkOperation
from apps.reader.services import sdk_runner

pytestmark = pytest.mark.django_db


def _unpriced_run(**tokens):
    return SdkOperation.objects.create(
        kind="assemble_context",
        prompt_hash="z" * 64,
        ok=False,
        error_class="Timeout",
        finished_at=timezone.now(),
        **tokens,
    )


class TestTheGapCountsEveryTokenColumn:
    def test_cache_tokens_reach_the_total(self) -> None:
        """The measured shape of a real cut-off run."""
        _unpriced_run(
            input_tokens=2, output_tokens=2,
            cache_read_tokens=11689, cache_write_tokens=2212,
        )

        gap = sdk_runner.today_unpriced()

        assert gap["total_tokens"] == 13905, (
            "the operator is told this run burned a handful of tokens when "
            "it burned nearly fourteen thousand"
        )
        assert gap["cache_read_tokens"] == 11689
        assert gap["cache_write_tokens"] == 2212

    def test_a_run_that_was_all_cache_reads_is_still_a_gap(self) -> None:
        """The `> 0` filter used to look at input/output only, so this run
        was indistinguishable from one that never reached the API."""
        _unpriced_run(cache_read_tokens=9000)

        gap = sdk_runner.today_unpriced()

        assert gap["runs"] == 1
        assert gap["total_tokens"] == 9000

    def test_a_run_that_burned_nothing_is_still_not_a_gap(self) -> None:
        """The other side of that filter, unchanged: a run that failed
        before touching the API is not spend the breaker is missing."""
        SdkOperation.objects.create(
            kind="chat", prompt_hash="q" * 64, ok=False,
            error_class="NotConfigured", finished_at=timezone.now(),
        )

        assert sdk_runner.today_unpriced()["runs"] == 0

    def test_nulls_do_not_break_the_total(self) -> None:
        """Most columns are nullable and most rows leave some unset."""
        _unpriced_run(input_tokens=5)

        assert sdk_runner.today_unpriced()["total_tokens"] == 5


class TestEveryConsumerUsesTheTotal:
    def test_the_gate_warning_reports_the_full_figure(self, monkeypatch, caplog) -> None:
        import logging

        _unpriced_run(input_tokens=2, output_tokens=2,
                      cache_read_tokens=11689, cache_write_tokens=2212)
        monkeypatch.setattr(sdk_runner.config, "anthropic_api_key", lambda: "sk-test")
        monkeypatch.setattr(sdk_runner.config, "daily_cost_cap", lambda: 10)

        with caplog.at_level(logging.WARNING):
            sdk_runner._check_gates(exempt_daily_cap=False)

        warning = next(
            r.getMessage() for r in caplog.records if "could not be priced" in r.getMessage()
        )
        assert "13905" in warning

    def test_the_template_renders_the_total(self) -> None:
        """Structural: the page had `input_tokens|add:output_tokens`
        inline, so fixing the function alone would leave it lying."""
        from pathlib import Path

        from django.conf import settings as dj

        html = (Path(dj.BASE_DIR) / "templates/ops/logs.html").read_text(encoding="utf-8")

        assert "unpriced.total_tokens" in html
        assert "unpriced.input_tokens|add:unpriced.output_tokens" not in html
