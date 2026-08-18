"""A wedged CLI must not hang the chat stream forever.

`stream_agent` checked the wall clock only AFTER a message arrived, so
the `await` for the next one was unbounded. A Claude CLI that wedged —
before producing anything, or between two events — hung the SSE response
indefinitely: the Send button stayed disabled, the ledger row stayed
`ok=None`, and the subprocess kept spending where the daily-cap breaker
could not see it. The non-streaming path uses `asyncio.wait_for` and was
never exposed to this.

These drive `stream_agent` against a stub SDK module, because the real
one shells out to the Claude CLI and costs money. What is under test is
this module's control flow, which is where the bug was.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from apps.events.models import SdkOperation
from apps.reader.services import sdk_runner

pytestmark = pytest.mark.django_db


# ---- a stub `claude_agent_sdk` -------------------------------------------


class StreamEvent:
    def __init__(self, event):
        self.event = event


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, name, input):
        self.name, self.input = name, input


class AssistantMessage:
    def __init__(self, content, model="stub-model"):
        self.content, self.model = content, model


class ResultMessage:
    def __init__(self, result="done", is_error=False):
        self.result = result
        self.is_error = is_error
        self.usage = {"input_tokens": 10, "output_tokens": 5}
        self.model_usage = None
        self.duration_ms = 12
        self.num_turns = 1
        self.total_cost_usd = 0.01
        self.subtype = None
        # Only the non-streaming path reads this, but the stub stands in
        # for the real message everywhere — leaving it off made `_drain`
        # raise AttributeError and report a transport failure.
        self.structured_output = None


def _delta(text):
    return StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


@pytest.fixture()
def stub_sdk(monkeypatch):
    """Install a fake `claude_agent_sdk` whose `query` the test supplies."""
    module = types.ModuleType("claude_agent_sdk")
    module.StreamEvent = StreamEvent
    module.TextBlock = TextBlock
    module.ToolUseBlock = ToolUseBlock
    module.AssistantMessage = AssistantMessage
    module.ResultMessage = ResultMessage
    module.query = None  # set per test
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)

    monkeypatch.setattr(sdk_runner, "_check_gates", lambda **kw: "sk-test")
    monkeypatch.setattr(sdk_runner, "_snapshot_options", lambda *a, **kw: object())
    monkeypatch.setattr(sdk_runner.config, "sdk_timeout_seconds", lambda: 1)
    return module


class _Stream:
    """An async iterator that records whether it was closed."""

    def __init__(self, messages, *, stall_after=None):
        self.messages = list(messages)
        self.stall_after = stall_after
        self.closed = False
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.stall_after is not None and self._i >= self.stall_after:
            await asyncio.sleep(3600)  # the wedged CLI
        if self._i >= len(self.messages):
            raise StopAsyncIteration
        msg = self.messages[self._i]
        self._i += 1
        return msg

    async def aclose(self):
        self.closed = True


async def _drain(**kw):
    out = []
    async for item in sdk_runner.stream_agent(
        kind="reader", tier="public", prompt="hi", append_system="", **kw
    ):
        out.append(item)
    return out


def _run(coro_fn):
    """Drive an async generator from a sync test, in THIS thread.

    `asyncio.run` would push every `sync_to_async` DB call onto asgiref's
    executor thread, which has its own connection — so the ledger rows
    would commit for real and leak into every later test instead of
    rolling back with this one. Under `async_to_sync`, `sync_to_async`
    with its default `thread_sensitive=True` runs the callable back in
    the calling thread, inside the test transaction.

    The 20s guard is the safety net for the bug under test: unfixed,
    `stream_agent` never returns at all.
    """
    from asgiref.sync import async_to_sync

    async def _guarded():
        return await asyncio.wait_for(coro_fn(), timeout=20)

    return async_to_sync(_guarded)()


class TestAWedgedCliDoesNotHang:
    def test_a_stream_that_never_yields_anything_times_out(self, stub_sdk):
        """The finding. Unfixed this never returns — the whole test would
        sit on the 20s guard rather than the 1s SDK timeout."""
        stream = _Stream([], stall_after=0)
        stub_sdk.query = lambda **kw: stream

        events = _run(_drain)

        kind, run = events[-1]
        assert kind == "result"
        assert run.error_class == "Timeout"

    def test_a_stream_that_stalls_midway_times_out(self, stub_sdk):
        stream = _Stream([_delta("hel"), _delta("lo")], stall_after=2)
        stub_sdk.query = lambda **kw: stream

        events = _run(_drain)

        assert ("delta", "hel") in events
        assert events[-1][1].error_class == "Timeout"

    def test_the_partial_text_is_kept(self, stub_sdk):
        stream = _Stream(
            [AssistantMessage([TextBlock("partial answer")])], stall_after=1
        )
        stub_sdk.query = lambda **kw: stream

        run = _run(_drain)[-1][1]

        assert run.text == "partial answer"

    def test_the_stream_is_closed_so_the_subprocess_is_reaped(self, stub_sdk):
        stream = _Stream([], stall_after=0)
        stub_sdk.query = lambda **kw: stream

        _run(_drain)

        assert stream.closed is True

    def test_the_ledger_row_is_finalized(self, stub_sdk):
        stream = _Stream([], stall_after=0)
        stub_sdk.query = lambda **kw: stream

        _run(_drain)

        op = SdkOperation.objects.latest("id")
        assert op.ok is False
        assert op.error_class == "Timeout"
        assert op.finished_at is not None


class TestNormalStreamingIsUnchanged:
    def test_deltas_are_yielded_then_the_result(self, stub_sdk):
        stream = _Stream(
            [
                _delta("he"),
                _delta("llo"),
                AssistantMessage([TextBlock("hello")]),
                ResultMessage(),
            ]
        )
        stub_sdk.query = lambda **kw: stream

        events = _run(_drain)

        assert [e for e in events if e[0] == "delta"] == [("delta", "he"), ("delta", "llo")]
        assert events[-1][0] == "result"
        assert events[-1][1].ok is True
        assert events[-1][1].text == "hello"

    def test_read_paths_are_collected(self, stub_sdk):
        stream = _Stream(
            [
                AssistantMessage(
                    [ToolUseBlock("Read", {"file_path": "/data/brain-views/public/a.md"})]
                ),
                ResultMessage(),
            ]
        )
        stub_sdk.query = lambda **kw: stream

        run = _run(_drain)[-1][1]

        assert run.read_paths == ["/data/brain-views/public/a.md"]

    def test_a_finished_stream_is_closed_too(self, stub_sdk):
        stream = _Stream([ResultMessage()])
        stub_sdk.query = lambda **kw: stream

        _run(_drain)

        assert stream.closed is True

    def test_cost_and_usage_reach_the_ledger(self, stub_sdk):
        stream = _Stream([ResultMessage()])
        stub_sdk.query = lambda **kw: stream

        _run(_drain)

        op = SdkOperation.objects.latest("id")
        assert op.ok is True
        assert float(op.cost_usd) == 0.01
        assert op.input_tokens == 10 and op.output_tokens == 5

    def test_an_sdk_error_is_still_degraded_not_raised(self, stub_sdk):
        class Boom:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("transport died")

        stub_sdk.query = lambda **kw: Boom()

        run = _run(_drain)[-1][1]

        assert run.ok is False
        assert run.error_class == "RuntimeError"
