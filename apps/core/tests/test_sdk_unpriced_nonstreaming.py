"""The non-streaming path had the same blind spot, and a worse version.

`test_sdk_unpriced_spend.py` closed this for `stream_agent`: a run cut
short never receives the `ResultMessage` carrying `total_cost_usd`, so
its cost is unknowable — but the raw stream events it *did* see carry
token counts, and banking those makes the gap visible to
`today_unpriced()`.

`_run_ledgered` — `assemble-context`, feed extraction, and the Settings
page's connection test — never got that treatment, because it asked the
SDK for `include_partial_messages=False`. There were no stream events to
bank. So a timed-out `assemble-context` recorded no cost (fine, nothing
can price it) and no tokens either, which means it did not even show up
in the figure whose entire job is to admit what the breaker cannot see.
An operator reading /ops/logs/ saw a quiet day.

Two halves, both tested here: the options have to ask for the events,
and the accumulator has to survive `asyncio.wait_for` cancelling the
coroutine that filled it.

Cost is still left NULL. That decision is unchanged and deliberate —
see the module docstring on `today_unpriced`.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
from asgiref.sync import async_to_sync

from apps.events.models import SdkOperation
from apps.reader.services import sdk_runner

from .test_sdk_stream_timeout import (  # noqa: F401 - stub_sdk is a fixture
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    _Stream,
    stub_sdk,
)
from .test_sdk_unpriced_spend import _mdelta, _start

pytestmark = pytest.mark.django_db


def _run(coro_fn):
    """Drive a coroutine in THIS thread — see `_run` in the stream tests.

    `asyncio.run` would put the `sync_to_async` ledger writes on asgiref's
    executor thread, which holds its own connection, so the rows would
    commit for real and leak into every later test.
    """
    async def _guarded():
        return await asyncio.wait_for(coro_fn(), timeout=20)

    return async_to_sync(_guarded)()


async def _agent():
    return await sdk_runner.run_agent_async(
        kind="reader", tier="public", prompt="hi", append_system=""
    )


# ---- half one: the options have to ask for the events --------------------


class TestTheOptionsAskForPartialMessages:
    """Without this the accumulator has nothing to accumulate. Asserted
    on the real `ClaudeAgentOptions`, since the field name is the SDK's."""

    def test_the_tier_locked_options_ask_for_them(self, tmp_path, monkeypatch):
        from apps.brain.services import snapshots

        monkeypatch.setattr(snapshots, "tier_dir", lambda tier: tmp_path)
        options = sdk_runner._snapshot_options("public", "sk-test", "reader", "")

        assert options.include_partial_messages is True

    def test_the_connection_probe_asks_for_them(self, monkeypatch):
        captured = {}

        async def spy(*, kind, prompt, options, prompt_hash_input, timeout_s, subject=None):
            captured["options"] = options
            return sdk_runner.RunResult(ok=True, text="OK")

        monkeypatch.setattr(sdk_runner, "_run_ledgered", spy)
        monkeypatch.setattr(sdk_runner, "_check_gates", lambda **kw: "sk-test")

        _run(lambda: sdk_runner.test_connection_async(candidate_key="sk-test"))

        assert captured["options"].include_partial_messages is True


# ---- half two: the counts have to survive the cancellation ---------------


class TestATimedOutNonStreamingRunRecordsItsTokens:
    def test_the_ledger_row_carries_real_counts(self, stub_sdk):
        """The finding. Unfixed, this row shows no tokens at all — and so
        `today_unpriced()` cannot see it either."""
        stream = _Stream([_start(input_tokens=100), _mdelta(60)], stall_after=2)
        stub_sdk.query = lambda **kw: stream

        _run(_agent)

        op = SdkOperation.objects.latest("id")
        assert op.error_class == "Timeout"
        assert op.input_tokens == 100
        assert op.output_tokens == 60
        assert op.cache_read_tokens == 20

    def test_the_gap_is_now_countable(self, stub_sdk):
        """What the tokens are actually for: the operator's blind-spot
        figure. Unfixed the row has no tokens, so it is filtered out."""
        stream = _Stream([_start(input_tokens=100), _mdelta(60)], stall_after=2)
        stub_sdk.query = lambda **kw: stream

        _run(_agent)

        gap = sdk_runner.today_unpriced()
        assert gap["runs"] == 1
        assert gap["input_tokens"] == 100 and gap["output_tokens"] == 60
        assert gap["cache_read_tokens"] == 20 and gap["cache_write_tokens"] == 5
        assert gap["total_tokens"] == 185

    def test_the_cost_stays_null_rather_than_invented(self, stub_sdk):
        stream = _Stream([_start(), _mdelta(60)], stall_after=2)
        stub_sdk.query = lambda **kw: stream

        _run(_agent)

        assert SdkOperation.objects.latest("id").cost_usd is None

    def test_a_transport_failure_keeps_what_it_saw(self, stub_sdk):
        """Not only timeouts — an SDK error mid-run burned tokens too."""
        class _Dies(_Stream):
            async def __anext__(self):
                if self._i >= len(self.messages):
                    raise RuntimeError("transport died")
                return await super().__anext__()

        stub_sdk.query = lambda **kw: _Dies([_start(input_tokens=70), _mdelta(9)])

        _run(_agent)

        op = SdkOperation.objects.latest("id")
        assert op.error_class == "RuntimeError"
        assert op.input_tokens == 70 and op.output_tokens == 9


class TestACompletedRunIsUnchanged:
    def test_the_result_message_still_wins(self, stub_sdk):
        """A ResultMessage is authoritative; the partials that led up to
        it must not overwrite it."""
        stream = _Stream([_start(input_tokens=999), _mdelta(999), ResultMessage()])
        stub_sdk.query = lambda **kw: stream

        run = _run(_agent)

        op = SdkOperation.objects.latest("id")
        assert op.input_tokens == 10 and op.output_tokens == 5
        assert float(op.cost_usd) == 0.01
        assert run.ok is True

    def test_the_stream_events_do_not_reach_the_answer(self, stub_sdk):
        """They are accounting only — the text still comes from the
        assistant messages."""
        stream = _Stream([
            _start(),
            StreamEvent({"type": "content_block_delta",
                         "delta": {"type": "text_delta", "text": "SHOULD NOT APPEAR"}}),
            AssistantMessage([TextBlock("the answer")]),
            ResultMessage(),
        ])
        stub_sdk.query = lambda **kw: stream

        run = _run(_agent)

        assert run.text == "the answer"

    def test_a_run_with_no_result_message_is_still_an_error(self, stub_sdk):
        stub_sdk.query = lambda **kw: _Stream([_start()])

        run = _run(_agent)

        assert run.ok is False
        assert run.error_class == "NoResultMessage"
