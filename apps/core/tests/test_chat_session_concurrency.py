"""Two tabs on one chat session must not corrupt it.

A turn inserts the user message, then replays recent history into the
prompt. The history query was "every message except the newest", which
is only that turn's own message while nothing else is writing. With two
turns in flight the second insert steals the exclusion, so a turn
replays a slice that contains its own new message (asking the model to
answer a question it can already see in the transcript) and omits the
other tab's.

The session totals were worse: read-modify-write off a `ChatSession`
loaded when the request began, so of two concurrent turns one update was
simply overwritten.

Three changes, tested here:

- a per-session lock, which REFUSES the second turn rather than queueing
  it — a turn is a 5-30s streamed run and a queued second tab reads as a
  hang;
- history excluded by primary key, so the prompt is right even when the
  lock is not there to help (it lives in the cache);
- totals incremented with `F()` in the database.

The turns are driven against the stub SDK from `test_sdk_stream_timeout`
— what is under test is this module's ordering, not the CLI.
"""
from __future__ import annotations

import asyncio

import pytest
from asgiref.sync import async_to_sync
from django.core.cache import cache

from apps.reader.models import ChatMessage, ChatSession
from apps.reader.services import chat

from .test_sdk_stream_timeout import (  # noqa: F401 - stub_sdk is a fixture
    AssistantMessage,
    ResultMessage,
    TextBlock,
    _delta,
    _Stream,
    stub_sdk,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def session():
    return ChatSession.objects.create(tier="public")


@pytest.fixture(autouse=True)
def clean_locks():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def answers(stub_sdk, monkeypatch):
    """Every turn answers instantly, and the sources lookup is skipped —
    it needs a snapshot directory this test has no use for.

    A real `content_block_delta` leads, so `run_turn` yields at least one
    `delta` and the disconnect test has a mid-stream point to abandon.
    """
    stub_sdk.query = lambda **kw: _Stream(
        [_delta("an "), AssistantMessage([TextBlock("an answer")]), ResultMessage()]
    )
    monkeypatch.setattr(chat, "_sources_from_paths", lambda tier, paths: [])
    return stub_sdk


def _drive(session, text):
    """Run one turn to completion and return its events."""
    async def _go():
        return [ev async for ev in chat.run_turn(session, text)]

    return async_to_sync(_go)()


def _kinds(events):
    return [k for k, _ in events]


class TestOnlyOneTurnAtATime:
    def test_a_second_turn_is_refused_while_the_first_holds_the_lock(
        self, session, answers
    ) -> None:
        """Asserted with the lock held rather than by racing two real
        turns: the refusal is the mid-flight state, and a race would
        pass either way once the loser finishes."""
        cache.add(chat.turn_lock_key(session.pk), "1", 300)

        events = _drive(session, "second tab")

        assert _kinds(events) == ["error"]
        assert "already has a turn running" in events[0][1]["message"]

    def test_the_refused_turn_writes_nothing(self, session, answers) -> None:
        """The refusal has to come BEFORE the user message is inserted,
        or the session accumulates messages that were never answered."""
        cache.add(chat.turn_lock_key(session.pk), "1", 300)

        _drive(session, "second tab")

        assert ChatMessage.objects.filter(session=session).count() == 0

    def test_the_lock_is_released_when_the_turn_finishes(self, session, answers) -> None:
        _drive(session, "hello")

        assert cache.get(chat.turn_lock_key(session.pk)) is None
        assert _kinds(_drive(session, "again")) == ["delta", "done"]

    def test_the_lock_is_released_when_the_turn_fails(self, session, stub_sdk) -> None:
        class _Boom:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("transport died")

        stub_sdk.query = lambda **kw: _Boom()

        _drive(session, "hello")

        assert cache.get(chat.turn_lock_key(session.pk)) is None

    def test_the_lock_is_released_when_the_client_disconnects(
        self, session, answers
    ) -> None:
        """Closing the tab mid-stream must not lock the session out for
        the lock's whole TTL."""
        async def _abandon():
            gen = chat.run_turn(session, "hello")
            await gen.__anext__()          # first delta, mid-stream
            await gen.aclose()             # the browser went away

        async_to_sync(_abandon)()

        assert cache.get(chat.turn_lock_key(session.pk)) is None

    def test_a_different_session_is_not_blocked(self, session, answers) -> None:
        other = ChatSession.objects.create(tier="public")
        cache.add(chat.turn_lock_key(session.pk), "1", 300)

        assert _kinds(_drive(other, "unrelated")) == ["delta", "done"]

    def test_the_lock_expires_rather_than_wedging_forever(
        self, session, answers, monkeypatch
    ) -> None:
        """A killed process leaves the claim behind; it has to have a
        bound, and that bound has to outlast the run it protects."""
        from apps.brainconfig import services as config

        monkeypatch.setattr(config, "sdk_timeout_seconds", lambda: 300)
        _drive(session, "hello")

        assert chat._lock_ttl() == 300 + chat.TURN_LOCK_GRACE_SECONDS


class TestTheReplayedHistoryIsThisTurnsHistory:
    def test_the_new_message_is_not_replayed_back_at_the_model(
        self, session, answers, monkeypatch
    ) -> None:
        seen = {}
        real = chat.compose_prompt
        monkeypatch.setattr(
            chat, "compose_prompt",
            lambda history, text: seen.setdefault("history", list(history)) or real(history, text),
        )
        _drive(session, "first")
        seen.clear()
        _drive(session, "second")

        contents = [m.content for m in seen["history"]]
        assert contents == ["first", "an answer"], contents

    def test_another_tabs_message_does_not_steal_the_exclusion(
        self, session, answers, monkeypatch
    ) -> None:
        """The finding, reproduced without a race: a message that lands
        between this turn's insert and its history read is the newest
        row, so "drop the newest" dropped the wrong one and this turn's
        own message came back as history."""
        seen = {}
        real = chat.compose_prompt

        def spy(history, text):
            seen["history"] = list(history)
            return real(history, text)

        monkeypatch.setattr(chat, "compose_prompt", spy)

        original_create = ChatMessage.objects.create

        def create_then_interleave(**kw):
            msg = original_create(**kw)
            if kw.get("role") == "user":
                # The other tab, arriving in the gap.
                original_create(session=session, role="user", content="other tab")
            return msg

        monkeypatch.setattr(ChatMessage.objects, "create", create_then_interleave)
        _drive(session, "mine")

        contents = [m.content for m in seen["history"]]
        assert "mine" not in contents, (
            "this turn replayed its own new message as history — the model is "
            "asked to answer a question already in the transcript"
        )


class TestTheTotalsAreNotLost:
    def test_two_sequential_turns_both_count(self, session, answers) -> None:
        _drive(session, "one")
        _drive(session, "two")

        session.refresh_from_db()
        assert session.total_input_tokens == 20  # 10 per stub ResultMessage
        assert session.total_output_tokens == 10
        assert float(session.total_cost_usd) == pytest.approx(0.02)

    def test_a_stale_in_memory_session_does_not_overwrite(
        self, session, answers
    ) -> None:
        """The finding. `stale` is the object a second request would be
        holding — loaded before the first turn, and unaware of it."""
        stale = ChatSession.objects.get(pk=session.pk)

        _drive(session, "one")
        _drive(stale, "two")

        session.refresh_from_db()
        assert session.total_input_tokens == 20, (
            "the second turn wrote back a total computed from a snapshot "
            "taken before the first turn ran"
        )
