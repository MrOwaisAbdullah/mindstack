"""The wizard's Verify clones on the worker, not inside the request.

`_step_read` called `setup_services.verify_read_access` inline. That is a
`git clone` with a 180s timeout, run inside a request served by a
gunicorn worker with a 60s one — and pressed at the exact moment it is
most likely to hang, because the operator has usually just pasted the
deploy key into GitHub and is finding out whether it took. The worker
killed the request, the browser showed a 502, and the session-stored
verdict was never written, so the page could not even say what had
happened.

`/ops/health/` already had this exact button and already ran it through
`jobs.enqueue`. This puts the wizard on the same job, which also means
the run appears on `/ops/tasks/` like every other background action.

The tests assert the mid-flight state — what the request *did*, and what
the page renders from the job record — because both the old and the new
version end in a redirect back to the same page.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache

from apps.brain.services import gitrepo
from apps.brainconfig import jobs, maintenance, setup_services

pytestmark = pytest.mark.django_db

VERIFY = maintenance.VERIFY_READ.name


@pytest.fixture
def operator(client):
    user = User.objects.create_user(
        "verify-op", password="x" * 20, is_staff=True, is_superuser=True
    )
    client.force_login(user)
    return user


@pytest.fixture(autouse=True)
def clean_job_record():
    """The job record lives in the cache, which outlives a transaction."""
    cache.delete(f"{jobs.CACHE_PREFIX}{VERIFY}")
    yield
    cache.delete(f"{jobs.CACHE_PREFIX}{VERIFY}")


@pytest.fixture(autouse=True)
def read_step_reachable(settings, monkeypatch):
    """A repo URL (or the step bounces to `repo`), and no clone on disk —
    `read_verified()` would otherwise reach for `git ls-remote`."""
    settings.BRAIN_REPO_URL = "git@github.com:someone/brain.git"
    monkeypatch.setattr(gitrepo, "is_valid_repo", lambda: False)


@pytest.fixture
def queue(monkeypatch):
    """Record what would go to django_q instead of sending it."""
    import django_q.tasks

    queued: list[str] = []
    monkeypatch.setattr(
        django_q.tasks, "async_task", lambda task, *a, **kw: queued.append(task)
    )
    return queued


def test_verify_does_not_clone_inside_the_request(client, operator, queue, monkeypatch) -> None:
    ran: list[str] = []

    def spy(url):
        ran.append(url)
        return setup_services.VerifyResult(ok=True, message="fake")

    monkeypatch.setattr(setup_services, "verify_read_access", spy)

    client.post("/setup/read/", {"action": "verify"})

    assert ran == [], (
        "the wizard ran a 180s git clone inside the web request; a gunicorn "
        "worker kills that at 60s and the verdict is never recorded"
    )
    assert queue == ["apps.brainconfig.maintenance.job_verify_read"]


def test_the_page_renders_what_the_worker_recorded(client, operator) -> None:
    """git's own stderr and the missing contract files are the point of
    this page, and `run()` carries neither — the job body stashes them."""
    jobs.update(
        VERIFY,
        state="failed",
        label="Verify read access failed",
        message="The server could not read that repository.",
        git_error="git@github.com: Permission denied (publickey).",
        missing=[],
        head="",
    )
    body = client.get("/setup/read/").content.decode()

    assert "The server could not read that repository." in body
    assert "Permission denied (publickey)." in body


def test_a_reachable_repo_that_is_not_a_brain_still_lists_its_gaps(
    client, operator
) -> None:
    jobs.update(
        VERIFY,
        state="failed",
        message="The server can reach that repository, but it isn't a brain yet",
        git_error="",
        missing=["CLAUDE.md", "INDEX.md"],
        head="abc123def456",
    )
    body = client.get("/setup/read/").content.decode()

    assert "CLAUDE.md" in body and "INDEX.md" in body
    assert "abc123def456" in body


def test_a_run_in_flight_says_so_and_watches_it(client, operator) -> None:
    jobs.mark_queued(VERIFY, "Verify read access…")
    body = client.get("/setup/read/").content.decode()

    assert "/setup/verify.json" in body, "the page has no way to notice it finished"
    assert "Checking" in body


def test_starting_a_run_drops_the_previous_verdict(client, operator, queue) -> None:
    """`mark_queued` used to merge, so last week's stderr sat on this
    week's record for as long as the new clone took."""
    jobs.update(VERIFY, state="failed", message="stale message",
                git_error="stale stderr", missing=["stale.md"], head="staleHEAD")

    client.post("/setup/read/", {"action": "verify"})
    body = client.get("/setup/read/").content.decode()

    for stale in ("stale message", "stale stderr", "stale.md", "staleHEAD"):
        assert stale not in body, f"{stale!r} survived into the new run"


def test_regenerating_the_key_clears_the_verdict(client, operator) -> None:
    """The verdict was about the key that just stopped existing."""
    jobs.update(VERIFY, state="done", message="The server can read your brain.")

    client.post("/setup/read/", {"action": "regenerate"})

    assert jobs.get(VERIFY) == {}


def test_a_second_press_does_not_queue_a_second_clone(client, operator, queue) -> None:
    client.post("/setup/read/", {"action": "verify"})
    client.post("/setup/read/", {"action": "verify"})

    assert len(queue) == 1, "two clones of the same repo, in parallel, on one worker"


def test_the_poll_endpoint_is_staff_only(client) -> None:
    User.objects.create_user("owner", password="x" * 20, is_staff=True, is_superuser=True)
    assert client.get("/setup/verify.json").status_code == 403


# ---- the worker half -----------------------------------------------------


def test_the_job_stashes_the_structured_outcome(monkeypatch) -> None:
    """What the page reads back has to be what the job writes down."""
    monkeypatch.setattr(gitrepo, "configured_url", lambda: "git@github.com:x/y.git")
    monkeypatch.setattr(
        setup_services,
        "verify_read_access",
        lambda url: setup_services.VerifyResult(
            ok=False,
            message="The server could not read that repository.",
            git_error="Permission denied (publickey).",
        ),
    )

    maintenance.job_verify_read()
    record = jobs.get(VERIFY)

    assert record["state"] == "failed"
    assert record["message"] == "The server could not read that repository."
    assert record["git_error"] == "Permission denied (publickey)."


def test_the_wizards_verify_is_the_same_job_the_tasks_page_lists(client, operator) -> None:
    """Structural: the ops rule is that background work shows up on
    /ops/tasks/. Sharing the health page's spec is how that happens."""
    assert maintenance.VERIFY_READ in maintenance.ALL_JOBS
    assert maintenance.VERIFY_READ.task == "apps.brainconfig.maintenance.job_verify_read"
