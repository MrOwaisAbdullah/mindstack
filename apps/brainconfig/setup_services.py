"""The work behind the wizard's buttons.

Two things here earn their keep:

- `normalise_repo_input` accepts what people actually paste (`me/brain`,
  a browser URL, an ssh remote) and, for GitHub, settles on the **ssh**
  form. That is not cosmetic: the app hands out an ssh deploy key, and an
  https remote would ignore it and fail on a private repo with an
  unhelpful credential prompt. The resolved URL is shown back to the user
  rather than silently swapped.

- `verify_read_access` does a real `git clone` into a throwaway
  directory and returns git's own stderr on failure. "Something went
  wrong" is useless here — the whole failure surface is *other people's*
  systems (key not installed yet, wrong repo name, repo is private and
  the key is on a different repo), and only git knows which.

- `verify_write_access` asks the remote whether this token may push,
  without pushing. Read access is not evidence of write access, and the
  gap between them is the single most confusing failure this app has:
  a token that clones fine surfaces as a *failed feed* long after setup,
  when the operator has stopped thinking about credentials.
"""
from __future__ import annotations

import base64
import logging
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: `owner/name`, the shorthand GitHub itself shows.
_SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_GITHUB_HTTPS_RE = re.compile(
    r"^https?://(?:[^@/]+@)?github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


class SetupError(RuntimeError):
    """A setup action failed in a way worth showing the operator verbatim."""


def normalise_repo_input(raw: str) -> str:
    """Turn pasted text into the git URL this server should use.

    Raises `SetupError` with a usable message rather than storing
    something that will fail later in a less obvious place.
    """
    value = (raw or "").strip()
    if not value:
        raise SetupError("Enter your repository, for example `your-name/brain`.")
    # A pasted browser URL often carries a branch path or a trailing bit.
    value = re.sub(r"/(?:tree|blob)/[^\s]*$", "", value)

    if _SHORTHAND_RE.match(value):
        owner, name = value.split("/")
        return f"git@github.com:{owner}/{name}.git"

    m = _GITHUB_HTTPS_RE.match(value)
    if m:
        # https -> ssh on purpose: the deploy key we issue is an ssh key.
        return f"git@github.com:{m.group('owner')}/{m.group('name')}.git"

    # `file://` is here for mirrors, air-gapped installs, and the tests that
    # exercise this whole path without a GitHub account.
    if value.startswith(("git@", "ssh://", "https://", "http://", "git://", "file://")):
        return value

    raise SetupError(
        f"{value!r} doesn't look like a repository. Use `owner/name`, or "
        "paste the full URL from your repository's page."
    )


def repo_web_url(git_url: str) -> str:
    """Best-effort https page for a git remote, for deep links. "" if unknown."""
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", git_url or "")
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    m = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", git_url or "")
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return ""


@dataclass
class VerifyResult:
    ok: bool
    message: str = ""
    git_error: str = ""
    head: str = ""
    missing_contract: list[str] = field(default_factory=list)


def verify_read_access(url: str) -> VerifyResult:
    """Clone the repo into a temp dir with the read credential. Real errors.

    Shallow, and thrown away immediately — this must be safe to press
    repeatedly while someone is still fiddling with GitHub's deploy-key
    page, so it never touches the live clone directory.
    """
    from apps.brain.services import gitrepo

    if not url:
        return VerifyResult(ok=False, message="No repository is configured yet.")

    tmp = Path(tempfile.mkdtemp(prefix="brain-verify-"))
    target = tmp / "clone"
    try:
        try:
            gitrepo._git("clone", "--depth", "1", "--single-branch", url, str(target), timeout=180)
        except gitrepo.BrainRepoError as exc:
            return VerifyResult(
                ok=False,
                message="The server could not read that repository.",
                git_error=str(exc),
            )
        missing = [p for p in gitrepo.CONTRACT_PATHS if not (target / p).exists()]
        head = ""
        try:
            head = gitrepo._git("rev-parse", "HEAD", cwd=target)
        except gitrepo.BrainRepoError:  # pragma: no cover - defensive
            pass
        if missing:
            return VerifyResult(
                ok=False,
                message=(
                    "The server can reach that repository, but it isn't a brain "
                    "yet — these files are missing."
                ),
                missing_contract=missing,
                head=head,
            )
        return VerifyResult(ok=True, message="The server can read your brain.", head=head)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _https_repo_base(url: str) -> str:
    """`git@host:owner/name.git` (or any remote form) → `https://host/owner/name`.

    Mirrors `apps.feeds.services.approval._tokenized_origin_url`, which is
    what the real push uses — the point of this check is to interrogate
    the same endpoint the push will, so the two must derive the same
    address. Returns "" for a remote with no https form (`file://`
    mirrors, used by tests and air-gapped installs).
    """
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?/?$", url or "") or re.match(
        r"^(?:ssh|https?)://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", url or ""
    )
    if not m:
        return ""
    return f"https://{m.group(1)}/{m.group(2)}"


def verify_write_access(url: str, pat: str) -> VerifyResult:
    """Can this token push to this repo? Asked without pushing anything.

    Git's smart-HTTP push begins by requesting the `git-receive-pack`
    service advertisement, and *that* is where the server decides whether
    you may write. So we make exactly that request and read the status
    code. Nothing is cloned, nothing is sent, no ref is touched — the
    operator can press this as often as they like while fixing a token's
    scopes.

    Measured against GitHub, which is the surface this diagnoses:

        | token / repo                     | upload-pack | receive-pack |
        | valid, push allowed              | 200         | 200          |
        | invalid or expired               | —           | 401          |
        | valid, no write on this repo     | 200         | 403          |

    The 403 row is the failure this exists to catch. It is the one a
    fine-grained PAT produces when the repo is simply not ticked under
    *Repository access* — the token authenticates perfectly, clones
    perfectly, and cannot push. Left unverified it reappears much later
    as a failed feed:

        remote: Write access to repository not granted.
        fatal: ... The requested URL returned error: 403

    A `--dry-run` push would be the more literal test, but it needs a
    local clone that does not exist yet at this point in the wizard, and
    it would make the operator wait for one just to answer a yes/no.
    """
    if not url:
        return VerifyResult(ok=False, message="No repository is configured yet.")
    if not pat:
        return VerifyResult(ok=False, message="Paste a token first, or skip this step.")

    base = _https_repo_base(url)
    if not base:
        return VerifyResult(
            ok=False,
            message=(
                f"{url} has no https form, so a token cannot be checked against "
                "it. This is normal for a local or mirrored remote."
            ),
        )

    probe = f"{base}.git/info/refs?service=git-receive-pack"
    # Git authenticates as `x-access-token:<pat>`; same here, so a token
    # that passes this passes the push.
    auth = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
    request = urllib.request.Request(
        probe,
        headers={"Authorization": f"Basic {auth}", "User-Agent": "git/brainoutside-setup"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status == 200:
                return VerifyResult(
                    ok=True, message="The server can push to your brain."
                )
            return VerifyResult(
                ok=False,
                message=f"Unexpected response from the repository ({response.status}).",
            )
    except urllib.error.HTTPError as exc:
        return VerifyResult(ok=False, **_write_failure(exc, base))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Network-shaped, not permission-shaped. Say so, so the operator
        # doesn't go re-cutting a token that was never the problem.
        return VerifyResult(
            ok=False,
            message="Could not reach the repository to check the token.",
            git_error=str(exc),
        )


def _write_failure(exc: "urllib.error.HTTPError", base: str) -> dict:
    """Turn a receive-pack status into something worth acting on."""
    if exc.code == 401:
        return {
            "message": (
                "That token was not accepted. It may be expired, revoked, or "
                "pasted incompletely."
            ),
            "git_error": "401 Unauthorized",
        }
    if exc.code == 403:
        return {
            "message": (
                "The token is valid but cannot write to this repository. For a "
                "fine-grained token, add this repository under Repository "
                "access and give it Contents: Read and write."
            ),
            "git_error": f"403 Forbidden — {base}",
        }
    if exc.code == 404:
        return {
            "message": (
                "This token cannot see that repository at all. Check the "
                "repository name, and that the token grants access to it."
            ),
            "git_error": f"404 Not Found — {base}",
        }
    return {
        "message": "The repository refused the write check.",
        "git_error": f"HTTP {exc.code}",
    }


# ---- the Build step ------------------------------------------------------


def run_build() -> None:
    """Clone (or reuse), index, and build snapshots, publishing progress.

    Runs on the Q2 worker. Every exit path writes a terminal state to the
    progress record — a Build that dies silently would leave the wizard
    spinning forever, which is worse than a visible failure.
    """
    from apps.brain.models import Entity
    from apps.brain.services import gitrepo, indexer, sync
    from apps.brainconfig import setup_state as state
    from apps.events.models import emit

    def step(n, label):
        state.set_progress(state="running", step=n, total=3, label=label, error="")
        log.info("setup: build step %s/3 — %s", n, label)

    try:
        step(1, "Cloning your brain")
        result = gitrepo.bootstrap()

        step(2, "Reading and indexing your notes")
        run = indexer.rebuild(trigger="setup")

        step(3, "Building the per-tier snapshots")
        sync.publish_snapshots(run)

        state.set_progress(
            state="done",
            step=3,
            label=f"{Entity.objects.count()} entities indexed",
            head=result.get("head", "")[:12],
            entities=Entity.objects.count(),
            added=run.added,
            error="",
        )
        emit("settings_change", key="setup.build", entities=Entity.objects.count())
        log.info("setup: build finished — %s entities", Entity.objects.count())
    except Exception as exc:
        log.exception("setup: build failed")
        state.set_progress(
            state="failed",
            label="Build failed",
            error=f"{exc.__class__.__name__}: {exc}",
        )
