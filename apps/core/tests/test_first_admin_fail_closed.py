"""A database blip must not announce that this server is unowned.

`needs_first_admin()` went through `_safe`, which reads a raising
predicate as *not done*. That is the right default for a progress
checklist and exactly inverted here: `not _safe(has_admin)` turns one
`OperationalError` into "no operator account exists".

On an established install that answer is load-bearing in two places:

- `SetupRequiredMiddleware` redirects **every** human-facing route to
  the wizard, so a connection-pool hiccup replaces the whole site with
  "Create your account";
- the wizard opens its account step to anyone (`step()` dispatches
  `account` without the staff guard precisely when this is true), so a
  passer-by who reloads during the blip is offered a superuser account
  on a server that already has one.

So this one predicate fails **closed** — assume an owner exists and let
the page that actually needs the database raise on its own terms. The
checklist keeps its fail-open reading, which is tested here too: the two
behaviours are different on purpose, not by accident.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import OperationalError

from apps.brainconfig import setup_state

pytestmark = pytest.mark.django_db


@pytest.fixture
def db_blip(monkeypatch):
    """`has_admin` raises the way a dropped connection makes it raise.

    Patched in two places because `_PREDICATES` captured the function
    object at import time — the checklist would otherwise keep calling
    the real one and see nothing wrong.
    """
    def boom() -> bool:
        raise OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(setup_state, "has_admin", boom)
    monkeypatch.setitem(setup_state._PREDICATES, "account", boom)


@pytest.fixture
def established():
    return User.objects.create_user(
        "owner", password="x" * 20, is_staff=True, is_superuser=True
    )


def test_a_blip_does_not_report_the_server_unowned(established, db_blip) -> None:
    assert setup_state.needs_first_admin() is False, (
        "a transient database error answered 'nobody owns this server'"
    )


def test_a_blip_does_not_reopen_the_account_step(client, established, db_blip) -> None:
    """The consequence that matters. Asserted on the *step*, not on the
    predicate, because this is the door the predicate unlocks."""
    response = client.get("/setup/account/")
    assert response.status_code == 302, (
        "the account step rendered to an anonymous visitor on an install "
        "that already has an operator"
    )
    assert response.url.startswith("/login/"), response.url


def test_a_blip_does_not_replace_the_site_with_the_wizard(
    client, established, db_blip
) -> None:
    response = client.get("/")
    redirected_to_setup = (
        response.status_code in (301, 302)
        and response.headers.get("Location", "").startswith("/setup/")
    )
    assert not redirected_to_setup, (
        "SetupRequiredMiddleware sent the front page to the wizard because "
        "one query failed"
    )


def test_the_checklist_still_reads_a_failed_probe_as_not_done(
    established, db_blip
) -> None:
    """`step_states()` drives a progress display, where "we could not
    check" is honestly closer to "not done" than to "done" — and nothing
    is unlocked by the answer. It keeps `_safe`."""
    account = next(s for s in setup_state.step_states() if s["slug"] == "account")
    assert account["done"] is False
