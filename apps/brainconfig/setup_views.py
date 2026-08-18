"""The first-run wizard (`/setup/`).

Auth model, which is the subtle part:

- While **no superuser exists** the account step — and only the account
  step — is open to anyone who can reach the server. That much is
  unavoidable for a zero-terminal install: something has to bootstrap
  the first identity, and the alternatives (a token printed in container
  logs, a `docker exec`) put the terminal back. The window is as small
  as we can make it: one page, which writes nothing but the account
  itself. Creating it closes the window permanently, the page says so
  out loud, and the app logs a warning on every boot while it is open.
  The other five steps are staff-only at all times — see
  `_require_staff`.
- Once an account exists, every step is staff-only, so the wizard cannot
  be used to reconfigure a running server by a passer-by.
"""
from __future__ import annotations

import json
import logging
import secrets

from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from apps.brain.services import gitcreds, gitrepo
from apps.brainconfig import jobs, maintenance
from apps.brainconfig import services as cfg
from apps.brainconfig import setup_services, setup_state

log = logging.getLogger(__name__)


def _ops_home() -> str:
    return reverse("brainconfig:dashboard")


def _context(request, slug: str, **extra) -> dict:
    states = setup_state.step_states()
    return {
        "steps": states,
        "current": slug,
        "current_index": next((i for i, s in enumerate(states) if s["slug"] == slug), 0),
        "total_steps": len(states),
        "setup_complete": setup_state.is_complete(states),
        **extra,
    }


def _require_staff(request):
    """None when allowed, else a redirect. Staff-only, on every step.

    The open window is exactly one page wide — the account step, which
    `step()` dispatches without calling this. It used to be six pages
    wide, because this answered "allowed" for the whole wizard while
    `needs_first_admin()` was true. Every other step *writes*: `repo`
    stores a setting, `read` mints the server's deploy key and renders
    the public half back, `write` and `claude` take credentials (and the
    Claude test spawns a CLI with the pasted key), `build` enqueues a
    clone on the worker. On an unowned install every one of those was
    reachable by anyone who could reach the port; two of them then
    crashed on `updated_by = AnonymousUser`, which is not a defence.
    """
    if request.user.is_authenticated and request.user.is_staff:
        return None
    if setup_state.needs_first_admin():
        # No account exists, so `/login/` is a dead end — there is
        # nothing to log in as. Send them to the one open page.
        return redirect("setup:step", slug="account")
    return redirect(f"{reverse('login')}?next={request.path}")


# ---- router --------------------------------------------------------------


def setup_home(request):
    """Send the operator to the first thing that still needs doing."""
    if setup_state.needs_first_admin():
        return redirect("setup:step", slug="account")
    guard = _require_staff(request)
    if guard is not None:
        return guard
    slug = setup_state.first_incomplete()
    if not slug:
        return redirect("setup:step", slug="build")  # the "all done" screen
    return redirect("setup:step", slug=slug)


@require_http_methods(["GET", "POST"])
def step(request, slug: str):
    if slug not in setup_state.STEP_BY_SLUG:
        raise Http404
    if slug != "account":
        guard = _require_staff(request)
        if guard is not None:
            return guard
    elif not setup_state.needs_first_admin():
        # The account step is done; don't offer to create a second one.
        guard = _require_staff(request)
        if guard is not None:
            return guard
        return redirect("setup:step", slug=setup_state.first_incomplete() or "build")

    return _HANDLERS[slug](request)


# ---- 1. account ----------------------------------------------------------


def _step_account(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_staff = True
            user.is_superuser = True
            user.email = (request.POST.get("email") or "").strip()
            user.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            log.warning(
                "setup: first operator account %r created — the open setup "
                "window is now closed", user.get_username(),
            )
            messages.success(request, "Account created. You're signed in.")
            return redirect("setup:step", slug="repo")
    else:
        form = UserCreationForm()
    return render(request, "setup/account.html", _context(request, "account", form=form))


# ---- 2. repo -------------------------------------------------------------

TEMPLATE_REPO_URL = "https://github.com/hassancs91/brainoutside-template/generate"


def _step_repo(request):
    error = ""
    if request.method == "POST":
        try:
            url = setup_services.normalise_repo_input(request.POST.get("repo", ""))
        except setup_services.SetupError as exc:
            error = str(exc)
        else:
            previous = gitrepo.configured_url()
            cfg.set_value("BRAIN_REPO_URL", url, actor=request.user)
            if gitrepo.normalize_remote(previous) != gitrepo.normalize_remote(url):
                # A different repo invalidates any earlier proof of access.
                setup_state.clear_read_verified()
            messages.success(request, f"Brain repository set to {url}")
            return redirect("setup:step", slug="read")

    current = gitrepo.configured_url()
    env_pinned = bool((dj_settings.BRAIN_REPO_URL or "").strip())
    return render(request, "setup/repo.html", _context(
        request, "repo",
        template_url=TEMPLATE_REPO_URL,
        current=current,
        env_pinned=env_pinned,
        error=error,
    ))


# ---- 3. read (deploy key) ------------------------------------------------


def _verify_result(record: dict) -> dict | None:
    """The Verify job's record in the shape `read.html` renders.

    None while it is queued, running, or has never run. `message`,
    `git_error`, `missing` and `head` are stashed by the job body
    (`maintenance.job_verify_read`) because `run()` itself only carries a
    label and an error string, and this page shows git's stderr verbatim
    and the missing contract files.
    """
    state = record.get("state")
    if state not in ("done", "failed"):
        return None
    return {
        "ok": state == "done",
        "message": record.get("message") or record.get("label") or "",
        "git_error": record.get("git_error") or "",
        "head": record.get("head") or "",
        "missing": record.get("missing") or [],
    }


def _step_read(request):
    url = gitrepo.configured_url()
    if not url:
        return redirect("setup:step", slug="repo")

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "regenerate":
            gitcreds.generate_keypair(actor=request.user)
            setup_state.clear_read_verified()
            # The last verdict was about the key that just stopped
            # existing. Leaving it on screen beside the new key would
            # read as a result for that key.
            jobs.clear(maintenance.VERIFY_READ.name)
            messages.warning(
                request,
                "New key generated. The old one no longer works — remove it "
                "from GitHub and add the new one.",
            )
            return redirect(request.path)
        if action == "verify":
            # On the worker, like the health page's identical button and
            # for the same reason: a clone is a 180s timeout inside a 60s
            # request. The page below watches the job record.
            started, message = jobs.enqueue(maintenance.VERIFY_READ)
            if not started:
                messages.info(request, message)
            return redirect(request.path)

    record = jobs.get(maintenance.VERIFY_READ.name)
    status = gitcreds.status()["read"]
    public_key = gitcreds.ensure_keypair(actor=request.user) if status["source"] != "file" else ""
    web = setup_services.repo_web_url(url)
    return render(request, "setup/read.html", _context(
        request, "read",
        repo_url=url,
        repo_web_url=web,
        add_key_url=f"{web}/settings/keys/new" if web else "",
        public_key=public_key,
        key_source=status["source"],
        verified=setup_state.read_verified(),
        verifying=record.get("state") == "running",
        result=_verify_result(record),
    ))


# ---- 4. write (PAT) ------------------------------------------------------


def _step_write(request):
    result = request.session.pop("write_verify_result", None)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "skip":
            setup_state.set_write_skipped(True)
            messages.info(
                request,
                "Skipped. Approved feeds will commit locally; the dashboard "
                "will tell you the brain is ahead of GitHub.",
            )
            return redirect("setup:step", slug="claude")
        pat = (request.POST.get("pat") or "").strip()
        if action == "verify":
            # Check the pasted token if there is one, otherwise whatever is
            # already stored — the same button has to work for "I just typed
            # this" and "is the thing I saved last week still good".
            outcome = setup_services.verify_write_access(
                gitrepo.configured_url(), pat or gitcreds.write_pat()
            )
            request.session["write_verify_result"] = {
                "ok": outcome.ok,
                "message": outcome.message,
                "git_error": outcome.git_error,
            }
            # Deliberately NOT saved on verify. A token that fails the check
            # should not end up stored because someone pressed the button to
            # find out.
            return redirect(request.path)
        if pat:
            gitcreds.set_write_pat(pat, actor=request.user)
            setup_state.set_write_skipped(False)
            # Save still checks, so the credential cannot pass through this
            # step unexamined — that was the whole gap. It is a warning, not
            # a block: a network blip must not strand setup, and the
            # operator may be fixing the token's scopes in another tab.
            outcome = setup_services.verify_write_access(
                gitrepo.configured_url(), pat
            )
            if outcome.ok:
                messages.success(request, "Write credential saved and verified.")
            else:
                messages.warning(
                    request,
                    f"Write credential saved, but it did not pass the check. "
                    f"{outcome.message} Approved feeds will fail to push until "
                    f"this is fixed.",
                )
            return redirect("setup:step", slug="claude")
        messages.error(request, "Paste a token, or choose 'Skip for now'.")
        return redirect(request.path)

    url = gitrepo.configured_url()
    web = setup_services.repo_web_url(url)
    status = gitcreds.status()["write"]
    return render(request, "setup/write.html", _context(
        request, "write",
        repo_web_url=web,
        token_url="https://github.com/settings/personal-access-tokens/new",
        configured=status["configured"],
        source=status["source"],
        result=result,
    ))


# ---- 5. claude -----------------------------------------------------------


def _step_claude(request):
    result = request.session.pop("claude_result", None)
    if request.method == "POST":
        key = (request.POST.get("key") or "").strip()
        action = request.POST.get("action", "")
        if action == "test":
            from apps.reader.services import sdk_runner

            # Test the PASTED key before anything stores it — the write
            # step's rule, applied here too. The step is marked done on
            # key *presence*, so the old order (save, then test) meant a
            # failing test still completed setup with a dead credential
            # and nothing anywhere said so. A blank paste probes the
            # stored key, the Settings-page behaviour.
            try:
                run = sdk_runner.test_connection(candidate_key=key)
                request.session["claude_result"] = {
                    "ok": run.ok,
                    "model": run.model,
                    "duration_ms": run.duration_ms,
                    "input_tokens": run.usage.get("input_tokens"),
                    "output_tokens": run.usage.get("output_tokens"),
                    "error": run.error_class,
                }
                if run.ok and key:
                    cfg.set_value("ANTHROPIC_API_KEY", key, actor=request.user)
            except Exception as exc:
                request.session["claude_result"] = {"ok": False, "error": str(exc)}
            return redirect(request.path)
        if action == "skip":
            # The step is optional and the page has always said so. Without
            # this the operator had no way off the page: the nav has no
            # links, Continue refused an empty field, and `is_complete()`
            # stayed False so /ops/ redirected back into the wizard.
            setup_state.set_claude_skipped(True)
            messages.info(
                request,
                "Skipped. Your notes are served over the API and MCP without "
                "it — chat, the reader agent and feed extraction stay off "
                "until you add a credential on the Settings page.",
            )
            return redirect("setup:step", slug="build")
        # Explicit continue: the operator chose to proceed without a
        # test — presence completes the step, as documented on the page.
        if key:
            cfg.set_value("ANTHROPIC_API_KEY", key, actor=request.user)
        if cfg.get("ANTHROPIC_API_KEY"):
            # A configured step is not a skipped one; the checklist would
            # otherwise keep calling it skipped forever.
            setup_state.set_claude_skipped(False)
            return redirect("setup:step", slug="build")
        messages.error(request, "Paste a credential first, or choose 'Skip for now'.")
        return redirect(request.path)

    key_set = bool(cfg.get("ANTHROPIC_API_KEY"))
    return render(request, "setup/claude.html", _context(
        request, "claude", key_set=key_set, result=result,
    ))


# ---- 6. build ------------------------------------------------------------


def _step_build(request):
    if request.method == "POST":
        if setup_state.build_running():
            messages.info(request, "A build is already running.")
            return redirect(request.path)
        setup_state.set_progress(
            state="running", step=0, total=3, label="Starting…", error="",
            token=secrets.token_hex(4),
        )
        try:
            from django_q.tasks import async_task

            async_task("apps.brainconfig.setup_services.run_build")
        except Exception as exc:
            setup_state.set_progress(
                state="failed", label="Could not reach the worker queue",
                error=str(exc),
            )
        return redirect(request.path)

    # Passed as a dict and rendered with |json_script, NOT interpolated as
    # a JSON string: `label` and `error` carry text from build and git
    # failures, and a `</script>` in there would end the block early and
    # kill the whole page's JS.
    return render(request, "setup/build.html", _context(
        request, "build",
        progress=setup_state.get_progress(),
        built=setup_state.brain_built(),
        ops_url=_ops_home(),
    ))


def verify_progress(request):
    """Polled by the Read step while the clone runs on the worker.

    State only. The verdict is rendered server-side from the same job
    record, so the page reloads once this stops saying "running" rather
    than reproducing the whole result block in JavaScript.
    """
    guard = _require_staff(request)
    if guard is not None:
        return JsonResponse({"error": "auth"}, status=403)
    record = jobs.get(maintenance.VERIFY_READ.name)
    return JsonResponse({
        "state": record.get("state", ""),
        "label": record.get("label", ""),
    })


def build_progress(request):
    """Polled by the Build step. Cheap, and safe to miss a tick."""
    guard = _require_staff(request)
    if guard is not None:
        return JsonResponse({"error": "auth"}, status=403)
    progress = setup_state.get_progress()
    return JsonResponse({
        **progress,
        "built": setup_state.brain_built(),
        "ops_url": _ops_home(),
    })


@require_POST
def finish(request):
    guard = _require_staff(request)
    if guard is not None:
        return guard
    setup_state.clear_progress()
    return redirect(_ops_home())


_HANDLERS = {
    "account": _step_account,
    "repo": _step_repo,
    "read": _step_read,
    "write": _step_write,
    "claude": _step_claude,
    "build": _step_build,
}
