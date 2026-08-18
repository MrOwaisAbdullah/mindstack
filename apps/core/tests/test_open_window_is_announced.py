"""SECURITY.md promised a warning about the open setup window. There was none.

The doc said, of the pre-account window:

    That window is one page wide (no other step is reachable), it closes
    permanently the moment you create the account, and the app logs a
    warning on every boot while it is still open.

The first two clauses were true and are tested elsewhere. The third was
not implemented anywhere: every use of `needs_first_admin` / `has_admin`
was redirect logic or a view guard, `brainconfig` had no `AppConfig.ready`
and no system check, and a fresh stack booted with zero users logged
nothing at all.

What is being protected: until the first operator account exists, anyone
who can reach the box can create it, and that account owns the ops UI —
every private note, every stored credential. The window is genuinely one
page wide and it does shut for good on account creation. The missing
piece was the nag, for the realistic failure where someone brings a VPS
up, gets pulled away, and leaves an unclaimed account form facing the
internet.

Emitted from the middleware rather than `AppConfig.ready()` on purpose:
`ready()` runs before the database is reliably available, and a query
there is a known way to make an app that cannot start. The middleware
already asks `needs_first_admin()` on the way through, so the answer is
free. Once per process, so an open window does not write a line per
request — a warning that scrolls is a warning nobody reads.
"""
from __future__ import annotations

import logging

import pytest
from django.contrib.auth.models import User

from apps.brainconfig import middleware as mw

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_process_flag():
    """The latch is module state, so it survives between tests."""
    mw.reset_open_window_warning()
    yield
    mw.reset_open_window_warning()


def _get(client, caplog, path="/"):
    with caplog.at_level(logging.WARNING, logger=mw.log.name):
        client.get(path)
    return caplog.text


class TestAnUnclaimedServerSaysSo:
    def test_a_request_with_no_operator_account_warns(self, client, caplog) -> None:
        text = _get(client, caplog)

        assert "setup" in text.lower()

    def test_the_warning_says_what_to_do(self, client, caplog) -> None:
        """A warning that names no action is decoration."""
        text = _get(client, caplog).lower()

        assert "account" in text
        assert any(w in text for w in ("anyone", "unclaimed", "reach"))

    def test_it_is_logged_once_per_process_not_once_per_request(
        self, client, caplog
    ) -> None:
        """An open window can last days. One line per request buries it."""
        with caplog.at_level(logging.WARNING, logger=mw.log.name):
            for _ in range(5):
                client.get("/")

        assert caplog.text.lower().count("unclaimed") == 1


class TestAClaimedServerIsQuiet:
    def test_no_warning_once_an_operator_exists(self, client, caplog) -> None:
        User.objects.create_superuser("op", "op@example.com", "pw-12345-xyz")

        assert "unclaimed" not in _get(client, caplog).lower()

    def test_a_non_superuser_does_not_count_as_an_owner(self, client, caplog) -> None:
        """`has_admin()` asks for a superuser. A plain account must not
        silence the warning — it cannot reach the ops UI either."""
        User.objects.create_user("someone", "s@example.com", "pw-12345-xyz")

        assert "unclaimed" in _get(client, caplog).lower()


class TestItNeverBreaksTheRequest:
    def test_a_failing_probe_does_not_500_the_page(self, client, monkeypatch, caplog) -> None:
        """The wizard has to stay reachable when things are broken; a
        warning about brokenness must not be what breaks it."""
        from apps.brainconfig import setup_state

        def boom():
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(setup_state, "needs_first_admin", boom)

        with caplog.at_level(logging.WARNING, logger=mw.log.name):
            with pytest.raises(RuntimeError):
                client.get("/")
