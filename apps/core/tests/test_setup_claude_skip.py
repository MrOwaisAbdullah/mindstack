"""Without an Anthropic key the wizard was a dead end, and so was /ops/.

Step 5 offered only "Test connection" and "Continue", and Continue with an
empty field answered "Paste a credential first." There was no skip, and
the step nav has no links, so the only way past was typing /setup/build/
into the address bar.

The trap was not the missing button. `claude` was not marked `optional`,
so `is_complete()` — which `SetupRequiredMiddleware` consults — stayed
False forever. Verified on a fresh install with a fully built, serving
brain: GET /ops/ and /ops/brain/ both 302'd to /setup/, /setup/ sent the
operator to `first_incomplete()` = "claude", and that page had no way
out. An operator could not reach their own dashboard.

The step is genuinely optional, which is why this is a fix and not a
policy change. Measured against a keyless install: ping, get-identity,
get-index, list-notes, get-note, get-lens and propose-feed all answer
200. Only assemble-context needs the key, and it already fails cleanly
with "reader unavailable: ANTHROPIC_API_KEY is not configured". The page
itself has always said "Everything else on this server works without it".

Two separate properties, because one without the other still traps
someone:

- `optional` is what stops the middleware bouncing a built install back
  into setup.
- the stored skip is what stops `first_incomplete()` returning "claude"
  every time, the same way the write step already works.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.brainconfig import setup_state

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_setting_cache():
    """`runtime_setting_store` is read-through cached, and `django_db`
    rolls back Postgres but not the cache. Without this, a skip stored by
    one test leaks into the next one's view of the default and the
    failure looks like a product bug — it cost a debugging round already.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _states(**done: bool) -> list[dict]:
    """Synthetic step_states(), so the aggregates can be tested without
    standing up a repo, a clone and a build."""
    return [
        {
            "step": s, "slug": s.slug, "title": s.title, "blurb": s.blurb,
            "optional": s.optional,
            "done": done.get(s.slug, False),
            "skipped": done.get(f"{s.slug}_skipped", False),
        }
        for s in setup_state.STEPS
    ]


ALL_BUT_CLAUDE = {"account": True, "repo": True, "read": True, "build": True}


class TestTheServerIsUsableWithoutAClaudeKey:
    def test_the_claude_step_is_optional(self) -> None:
        assert setup_state.STEP_BY_SLUG["claude"].optional is True

    def test_a_built_install_without_a_key_counts_as_complete(self) -> None:
        """This is the one the middleware reads. False here is what made
        /ops/ redirect to /setup/ on a working brain."""
        assert setup_state.is_complete(_states(**ALL_BUT_CLAUDE)) is True

    def test_an_unbuilt_install_is_still_incomplete(self) -> None:
        """Optional must not mean "nothing is required any more"."""
        assert setup_state.is_complete(
            _states(account=True, repo=True, read=True)
        ) is False


class TestTheWizardStopsSendingYouBack:
    def test_claude_is_offered_when_nothing_is_set(self) -> None:
        """Optional still means offered — the operator should be asked
        once, exactly as the write step is."""
        assert setup_state.first_incomplete(_states(account=True, repo=True, read=True,
                                                    write_skipped=True)) == "claude"

    def test_a_skipped_claude_is_not_the_next_step(self) -> None:
        states = _states(account=True, repo=True, read=True,
                         write_skipped=True, claude_skipped=True)

        assert setup_state.first_incomplete(states) == "build"

    def test_step_states_reports_the_skip(self) -> None:
        setup_state.set_claude_skipped(True)

        row = {s["slug"]: s for s in setup_state.step_states()}["claude"]

        assert row["skipped"] is True

    def test_nothing_is_skipped_by_default(self) -> None:
        row = {s["slug"]: s for s in setup_state.step_states()}["claude"]

        assert row["skipped"] is False
        assert setup_state.claude_skipped() is False


class TestTheStepItself:
    @pytest.fixture()
    def staff(self, client):
        user = User.objects.create_superuser("op", "op@example.com", "pw-12345-xyz")
        client.force_login(user)
        return user

    def test_posting_skip_moves_on_and_records_the_choice(self, client, staff) -> None:
        resp = client.post(reverse("setup:step", args=["claude"]), {"action": "skip"})

        assert resp.status_code == 302
        assert resp["Location"].endswith("/build/")
        assert setup_state.claude_skipped() is True

    def test_the_page_offers_a_skip_control(self, client, staff) -> None:
        """A step nobody can leave is the bug. Assert the way out is on
        the page, not merely reachable by URL."""
        body = client.get(reverse("setup:step", args=["claude"])).content.decode()

        assert 'value="skip"' in body

    def test_continue_without_a_key_still_asks_for_one(self, client, staff) -> None:
        """Skipping must be a deliberate choice, not what Continue does
        silently — a stored credential is the difference between a working
        chat and a broken one."""
        resp = client.post(reverse("setup:step", args=["claude"]), {"action": "continue", "key": ""})

        assert resp.status_code == 302
        assert resp["Location"].endswith("/claude/")
        assert setup_state.claude_skipped() is False

    def test_saving_a_key_clears_a_previous_skip(self, client, staff) -> None:
        """Otherwise the checklist keeps calling a configured step skipped."""
        setup_state.set_claude_skipped(True)

        client.post(reverse("setup:step", args=["claude"]),
                    {"action": "continue", "key": "sk-ant-test-value"})

        assert setup_state.claude_skipped() is False
