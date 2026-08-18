"""What the first-run wizard knows about how far setup has got.

**Step completion is derived from real state, not from a stored cursor.**
A cursor drifts: it says "deploy key installed" after someone rotates the
key, or "built" after the volume is wiped. Deriving means the wizard is
self-healing — delete the clone and the Build step lights up again — and
it means the same functions can drive the dashboard's setup-health panel,
so the two can never disagree about whether setup is finished.

Only two things genuinely cannot be derived, and both are decisions
rather than facts, so both are stored explicitly:

- that the operator *chose to skip* the write credential;
- that a Verify actually succeeded against a specific repo URL. A clone
  can be absent while the credential is perfectly good (nothing has
  cloned yet), so "did the key work" has to be remembered — but it is
  remembered *with the URL it was proved against*, so pointing the app at
  a different repo correctly un-verifies it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from apps.core import runtime_setting_store as store

log = logging.getLogger(__name__)

KEY_WRITE_SKIPPED = "setup.write_skipped"
KEY_CLAUDE_SKIPPED = "setup.claude_skipped"
KEY_READ_VERIFIED_URL = "setup.read_verified_url"

#: Steps an operator may decline. The stored decision lives under
#: `setup.<slug>_skipped` — see the module docstring on why a *decision*
#: cannot be derived the way a fact can.
SKIPPABLE = ("write", "claude")

#: The Build step is one of the named ops jobs (`jobs.py`), so it shows up
#: on the Tasks page next to every other background action instead of
#: having its own private progress channel.
BUILD_JOB = "setup_build"


@dataclass(frozen=True)
class Step:
    slug: str
    title: str
    blurb: str
    optional: bool = False


STEPS: tuple[Step, ...] = (
    Step("account", "Create your account",
         "One operator account. This page is open to anyone until you do."),
    Step("repo", "Create your brain",
         "A normal GitHub repository full of markdown files. You own it."),
    Step("read", "Let the server read it",
         "Install a read-only deploy key so the server can clone and pull."),
    Step("write", "Let the server write back",
         "A token so approved feeds can be pushed to GitHub.", optional=True),
    # Optional, and measured rather than assumed: on a keyless install
    # ping, get-identity, get-index, list-notes, get-note, get-lens and
    # propose-feed all answer 200. Only assemble-context needs the key,
    # and it says so cleanly. Marking it required made `is_complete()`
    # permanently False, and the middleware bounced a fully built brain
    # back into the wizard — an operator could not reach their dashboard.
    Step("claude", "Connect Claude",
         "An API key, or a Claude subscription token — no API billing.",
         optional=True),
    Step("build", "Build your brain",
         "Clone, index, and materialise the per-tier snapshots."),
)

STEP_BY_SLUG = {s.slug: s for s in STEPS}


# ---- individual step predicates -----------------------------------------


def has_admin() -> bool:
    from django.contrib.auth.models import User

    return User.objects.filter(is_superuser=True).exists()


def repo_configured() -> bool:
    from apps.brain.services import gitrepo

    return bool(gitrepo.configured_url())


def read_verified() -> bool:
    """True when this exact repo URL has been proved readable.

    Either a Verify run said so, or a valid clone of that same repo is
    already sitting on the volume — which is the case for anyone who
    mounted a clone themselves, and they should not be asked to install
    a deploy key to prove something already true.
    """
    from apps.brain.services import gitrepo

    url = gitrepo.configured_url()
    if not url:
        return False
    want = gitrepo.normalize_remote(url)
    if store.get_str(KEY_READ_VERIFIED_URL, "") == want:
        return True
    return gitrepo.is_valid_repo() and gitrepo.origin_probe()["ok"]


def mark_read_verified() -> None:
    from apps.brain.services import gitrepo

    store.set_value(KEY_READ_VERIFIED_URL, gitrepo.normalize_remote(gitrepo.configured_url()))


def clear_read_verified() -> None:
    """Drop the proof of read access — after a repo change or key rotation."""
    store.set_value(KEY_READ_VERIFIED_URL, "")


def write_configured() -> bool:
    from apps.brain.services import gitcreds

    return bool(gitcreds.status()["write"]["configured"])


def write_skipped() -> bool:
    return store.get_str(KEY_WRITE_SKIPPED, "") == "1"


def set_write_skipped(skipped: bool) -> None:
    store.set_value(KEY_WRITE_SKIPPED, "1" if skipped else "")


def claude_skipped() -> bool:
    return bool(store.get_str(KEY_CLAUDE_SKIPPED, ""))


def set_claude_skipped(skipped: bool) -> None:
    store.set_value(KEY_CLAUDE_SKIPPED, "1" if skipped else "")


def claude_configured() -> bool:
    from apps.brainconfig import services as cfg

    return bool(cfg.get("ANTHROPIC_API_KEY"))


def brain_built() -> bool:
    """A clone we can read, and at least one successful index run over it.

    This used to require `Entity.objects.exists()`, which trapped anyone
    whose repo indexed to zero entities: the build job reported done, the
    step stayed incomplete forever, and `SetupRequiredMiddleware` bounced
    every /ops/ request back to a wizard that had just cleared its own
    progress. There was no way out and nothing said what was wrong.

    Zero entities is a legitimate state — a repo whose notes are all
    drafts, or a brand-new brain someone hasn't written into yet. The
    honest signal that the build worked is that the indexer ran and
    succeeded, so that is what we check. `/setup/build/` still tells the
    operator when the count is zero, because it usually means the
    frontmatter isn't what they think.
    """
    from apps.brain.models import Entity, SyncRun
    from apps.brain.services import gitrepo

    if not gitrepo.is_valid_repo():
        return False
    return SyncRun.objects.filter(ok=True).exists() or Entity.objects.exists()


_PREDICATES = {
    "account": has_admin,
    "repo": repo_configured,
    "read": read_verified,
    "write": write_configured,
    "claude": claude_configured,
    "build": brain_built,
}


# ---- aggregate ----------------------------------------------------------


_SKIP_FLAGS = {"write": write_skipped, "claude": claude_skipped}


def step_states() -> list[dict]:
    """Every step with `done` / `skipped`, in order."""
    out = []
    for s in STEPS:
        done = _safe(_PREDICATES[s.slug])
        out.append({
            "step": s,
            "slug": s.slug,
            "title": s.title,
            "blurb": s.blurb,
            "optional": s.optional,
            "done": done,
            # Was hardcoded to the write step. A second skippable step
            # made that a silent no-op — the flag stored, the checklist
            # ignoring it, `first_incomplete()` returning the step forever.
            "skipped": s.slug in SKIPPABLE and not done and _safe(_SKIP_FLAGS[s.slug]),
        })
    return out


def _safe(fn) -> bool:
    """A predicate that raises (DB down, clone missing) reads as not-done.

    The wizard's job is to be reachable when things are broken, so a
    failing probe must not 500 the page that would fix it.
    """
    try:
        return bool(fn())
    except Exception:
        return False


def first_incomplete(states: list[dict] | None = None) -> str:
    """Where the wizard should send someone next — optional steps included.

    A first-time operator should be *offered* the write credential, so an
    optional step still stops navigation here until it is done or
    explicitly skipped.
    """
    states = states if states is not None else step_states()
    for s in states:
        if not s["done"] and not s["skipped"]:
            return s["slug"]
    return ""


def is_complete(states: list[dict] | None = None) -> bool:
    """Is the server usable — i.e. is every REQUIRED step done?

    Optional steps are excluded on purpose. This drives the middleware,
    so counting them would drag every existing installation that never
    configured a write credential back into the wizard, which is exactly
    the opposite of what "optional" is supposed to mean. A missing
    optional piece belongs on the dashboard's health panel, not in a
    redirect.
    """
    states = states if states is not None else step_states()
    return all(s["done"] for s in states if not s["optional"])


def needs_first_admin() -> bool:
    """No operator account exists — the app is unusable and unowned.

    Deliberately **not** `_safe`. `_safe` reads a raising predicate as
    not-done, which is right for a checklist and inverted here: `not
    _safe(has_admin)` turns one `OperationalError` into "nobody owns
    this server". That answer unlocks things. `SetupRequiredMiddleware`
    redirects every human-facing route to the wizard on it, and
    `setup_views.step()` dispatches the account step without its staff
    guard on it — so a connection-pool hiccup would replace an
    established install with "Create your account", offered to whoever
    happened to be looking.

    So this one fails closed: assume an owner exists, and let the page
    that genuinely needs the database raise on its own terms.
    """
    try:
        return not has_admin()
    except Exception:
        log.exception(
            "setup: could not determine whether an operator account exists; "
            "assuming one does"
        )
        return False


# ---- Build-step progress ------------------------------------------------


def set_progress(**fields) -> None:
    from apps.brainconfig import jobs

    jobs.update(BUILD_JOB, **fields)


def get_progress() -> dict:
    from apps.brainconfig import jobs

    return jobs.get(BUILD_JOB)


def clear_progress() -> None:
    from apps.brainconfig import jobs

    jobs.clear(BUILD_JOB)


def build_running() -> bool:
    from apps.brainconfig import jobs

    return jobs.is_running(BUILD_JOB)
