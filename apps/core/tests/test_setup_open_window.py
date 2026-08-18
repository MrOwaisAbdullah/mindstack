"""The pre-admin window is one page wide, not six.

`/setup/` is reachable by anyone before the first operator account
exists — it has to be, or a zero-terminal install cannot bootstrap its
own identity. The docstring on `setup_views` describes that window as
covering the **account step**. `_require_staff` implemented it as
covering *every* step, because it answered "allowed" for the whole
wizard whenever `needs_first_admin()` was true.

So on a fresh install, before anyone owns the server, a passer-by could:

- point `BRAIN_REPO_URL` at a repository of their choosing;
- make the server mint a deploy key and show them the public half;
- start the Build job on the worker.

Two of those paths also 500 on the way: `cfg.set_value(actor=...)` and
`gitcreds.generate_keypair(actor=...)` stamp `updated_by`, which is a FK
to `User`, and `request.user` is `AnonymousUser`.

These tests assert the **mid-flight** state — what was stored, minted, or
enqueued — because every one of these requests ends in a redirect either
way, and a status-code test would pass against the bug.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.brain.services import gitcreds
from apps.brainconfig import services as cfg

pytestmark = pytest.mark.django_db


@pytest.fixture
def anon(client):
    """A client that reports a 500 instead of re-raising it.

    Two of the paths under test crash rather than redirect. Letting the
    exception escape would still fail the test, but as an error with a
    traceback about foreign keys rather than a statement about who is
    allowed to configure this server.
    """
    client.raise_request_exception = False
    return client


@pytest.fixture
def repo_configured():
    """The read step returns to `repo` unless a URL is set."""
    cfg.set_value("BRAIN_REPO_URL", "git@github.com:someone/brain.git")


def test_the_account_step_is_open_before_the_first_admin(anon) -> None:
    """The bootstrap path itself. Everything below narrows the window;
    this pins the one hole that has to stay open."""
    assert not User.objects.filter(is_superuser=True).exists()
    response = anon.get("/setup/account/")
    assert response.status_code == 200


def test_a_stranger_cannot_point_the_server_at_their_own_repo(anon) -> None:
    response = anon.post("/setup/repo/", {"repo": "attacker/brain"})
    assert cfg.get("BRAIN_REPO_URL") == "", (
        "an anonymous POST rewrote the brain repository on an unowned "
        "install — the next operator inherits someone else's brain"
    )
    assert response.status_code != 500, (
        "the step was open and then crashed on `updated_by = AnonymousUser`; "
        "refusing it is the fix, not relying on the crash"
    )


def test_a_stranger_cannot_make_the_server_mint_a_deploy_key(
    anon, repo_configured
) -> None:
    response = anon.get("/setup/read/")
    assert gitcreds.public_key() == "", (
        "an anonymous GET generated the server's deploy key and rendered "
        "the public half back to the caller"
    )
    assert response.status_code != 500


def test_a_stranger_cannot_start_the_build_job(anon, monkeypatch) -> None:
    import django_q.tasks

    queued: list[str] = []
    monkeypatch.setattr(
        django_q.tasks, "async_task", lambda task, *a, **kw: queued.append(task)
    )
    anon.post("/setup/build/", {})
    assert queued == [], "an anonymous POST enqueued work on the worker"


def test_every_step_but_account_sends_a_stranger_to_the_account_step(anon) -> None:
    """Not to `/login/`: no account exists, so the login page is a dead
    end. The one page that can move them forward is the account step."""
    for slug in ("repo", "read", "write", "claude", "build"):
        response = anon.get(f"/setup/{slug}/")
        assert response.status_code == 302, f"/setup/{slug}/ was open"
        assert response.url == "/setup/account/", (
            f"/setup/{slug}/ sent an anonymous visitor to {response.url}"
        )


def test_once_an_admin_exists_a_stranger_is_sent_to_login(anon) -> None:
    """The established install keeps its old behaviour — a login prompt
    with a `next`, not a redirect to a step that no longer exists."""
    User.objects.create_user("owner", password="x" * 20, is_staff=True, is_superuser=True)
    response = anon.get("/setup/repo/")
    assert response.status_code == 302
    assert response.url.startswith("/login/"), response.url
    assert "next=/setup/repo/" in response.url
