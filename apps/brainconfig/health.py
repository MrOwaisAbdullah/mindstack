"""Setup health — the things nobody reads the docs page to find out.

The dashboard shows whatever is not OK; `/ops/health/` shows everything
plus the repair buttons. Two principles:

- **Say what we actually know.** We cannot see a Tailscale tunnel or a
  Cloudflare Access policy from inside the container, so the exposure
  check reports what is true — that *this application* is not restricting
  access — and says the quiet part: if you have a network boundary in
  front, this check can't see it.
- **Every warning names its fix**, as a button here or a concrete
  instruction. A warning an operator cannot act on is just noise, and
  noise is what makes people stop reading warnings.

`level` is one of danger / warn / ok / info.
"""
from __future__ import annotations

import ipaddress
import logging

from django.conf import settings

from apps.core import runtime_setting_store as store
from apps.core.security.client_ip import client_ip as _resolve_client_ip
from apps.core.security.client_ip import peer_ip, trusted_proxy_header

log = logging.getLogger(__name__)

KEY_BACKUP_ACK = "health.backup_acknowledged"


def _check(id, level, title, detail, **extra) -> dict:
    return {"id": id, "level": level, "title": title, "detail": detail, **extra}


def _client_ip(request) -> str:
    """The resolved caller address, as every per-IP control now sees it.

    This used to read `X-Forwarded-For` directly, which any caller can
    set — so a request claiming `X-Forwarded-For: 127.0.0.1` could talk
    the exposure check into reporting "local install" and hide the
    warning it exists to raise. It now goes through the same trusted-proxy
    resolution as the lockout and the allowlist, so what this page prints
    is what those controls actually enforce on.
    """
    return _resolve_client_ip(request) or ""


def _looks_local(value: str) -> bool:
    value = (value or "").split("%")[0].strip().strip("[]").lower()
    if value in ("localhost", "127.0.0.1", "::1", ""):
        return True
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False


def _is_local_request(request) -> bool:
    """Is this plausibly a developer on their own machine?

    Neither signal is trustworthy alone: the Host header is caller-supplied,
    and behind a reverse proxy REMOTE_ADDR is the proxy's own private bridge
    address, which would make every containerised deployment look local. So
    BOTH have to look local. The bias is deliberate — a spurious "your ops
    UI is exposed" warning costs someone ten seconds, while failing to warn
    costs them their private notes.

    Reads HTTP_HOST raw rather than `get_host()`, which raises DisallowedHost
    for a host outside ALLOWED_HOSTS — that would turn the single most
    important check on this page into "could not run the exposure check".
    """
    host = (request.META.get("HTTP_HOST") or "").rsplit(":", 1)[0]
    return _looks_local(host) and _looks_local(_client_ip(request))


# ---- individual checks ---------------------------------------------------


def _address_detail(request) -> str:
    """Explain which address the per-IP controls see, and why.

    An operator behind a proxy cannot configure `TRUSTED_PROXY_IPS`
    without knowing the peer address their proxy connects from, and that
    value is only observable from a real request — so this prints it.
    The "configured but not trusted" case is called out explicitly
    because it looks identical to working from the outside while every
    per-IP control silently still buckets on the proxy.
    """
    resolved = _client_ip(request) or "unknown"
    peer = peer_ip(request) or "unknown"
    header = trusted_proxy_header()

    if not header:
        return (
            f"Requests reach this app from {peer}. No trusted proxy header is "
            "configured, so that is the address every per-IP control uses. If "
            "you are behind a CDN or reverse proxy, that is the PROXY, not the "
            "caller — set TRUSTED_PROXY_IP_HEADER (e.g. CF-Connecting-IP) and "
            f"TRUSTED_PROXY_IPS (containing {peer}) so they see real callers."
        )
    if resolved == peer:
        return (
            f"{header} is configured, but requests are arriving from {peer}, "
            "which is not listed in TRUSTED_PROXY_IPS — so the header is being "
            f"ignored and per-IP controls are bucketing every caller as {peer}. "
            f"Add {peer} to TRUSTED_PROXY_IPS."
        )
    return (
        f"Your address is {resolved}, read from the {header} header sent by the "
        f"trusted proxy at {peer}."
    )


def check_exposure(request) -> dict:
    """The one that actually protects novices (SETUP-DESIGN.md Step 2)."""
    from apps.core.runtime_settings import get_admin_ip_allowlist

    allowlist = get_admin_ip_allowlist()
    if allowlist:
        return _check(
            "exposure", "ok", "Ops UI is IP-restricted",
            f"Only {', '.join(allowlist)} can reach the ops pages. "
            + _address_detail(request),
        )
    if _is_local_request(request):
        return _check(
            "exposure", "info", "Ops UI is unrestricted (local install)",
            "No IP allowlist is set, but you are reaching this over a local "
            "address, so this is probably a development machine.",
        )
    return _check(
        "exposure", "danger", "Your ops UI is reachable from the public internet",
        "This page approves feeds and can read every private note, and "
        "nothing in this application is restricting who reaches it. Put it "
        "behind Tailscale or Cloudflare Access, or set ADMIN_IP_ALLOWLIST. "
        + _address_detail(request)
        + " If you already have a network boundary in front of this server, "
        "this check cannot see it — it only knows about the app's own "
        "allowlist.",
        fix_hint="ADMIN_IP_ALLOWLIST",
    )


def check_unreadable_secrets() -> dict:
    """Did FIELD_ENCRYPTION_KEY change out from under the stored secrets?

    `AppSetting.value` swallows `InvalidToken` and returns "" so one bad
    row can't break every reader — which means a lost encryption key looks
    exactly like "my settings were wiped". This is the check that tells
    the truth about that.
    """
    from apps.brainconfig.models import AppSetting

    broken = [
        row.key
        for row in AppSetting.objects.exclude(value_encrypted="")
        if not row.value
    ]
    if not broken:
        return _check("secrets", "ok", "Stored credentials readable",
                      "Every encrypted setting decrypts with the current key.")
    return _check(
        "secrets", "danger",
        f"{len(broken)} stored credential(s) cannot be decrypted",
        "These were encrypted with a different FIELD_ENCRYPTION_KEY: "
        f"{', '.join(sorted(broken))}. Either restore the key (it lives in "
        "boot-secrets.json on the state volume — restore that file from a "
        "backup), or re-enter each value on the Settings page. They are not "
        "recoverable any other way.",
    )


def check_clone() -> dict:
    from apps.brain.services import gitrepo

    probe = gitrepo.status_probe()
    if not probe.get("valid"):
        return _check("clone", "danger", "No usable clone of your brain",
                      f"Nothing valid at {probe.get('dir')}. Build it from the "
                      "setup page, or replace the clone.",
                      action="replace_clone")
    # Before the contract check on purpose: a clone stopped mid-rebase can
    # read as contractless, and that diagnosis sends the operator to
    # "Replace the clone", which then refuses because the tree is dirty —
    # a dead end. This one names the actual problem and has a way out.
    if probe.get("rebase_in_progress"):
        return _check(
            "clone", "danger", "The clone is stuck mid-rebase",
            "A pull hit a conflict and left the rebase unfinished, so every "
            "later sync fails and the brain being served is frozen. The next "
            "sync aborts it automatically — run Pull from GitHub to trigger "
            "one, then check whether an approved feed committed here but "
            "never pushed.",
            action="pull_now",
        )
    if not probe.get("contract_ok"):
        return _check("clone", "danger", "The clone is missing contract files",
                      "Missing: " + ", ".join(probe.get("contract_missing", [])) +
                      ". The server refuses to serve a repository that isn't a "
                      "brain.", action="replace_clone")
    if not probe.get("origin_ok", True):
        origin = probe.get("origin", {})
        return _check(
            "clone", "danger", "The clone is not the repository you configured",
            f"Configured: {origin.get('configured')}. On disk: "
            f"{origin.get('actual') or 'no origin'}. The server is refusing to "
            "serve rather than answer from the wrong mind.",
            action="replace_clone",
        )
    return _check("clone", "ok", "Brain clone healthy",
                  f"HEAD {str(probe.get('head', ''))[:12]}")


def check_contract_version() -> dict:
    """Does the clone speak the contract this server was built against?

    Never "danger". Your brain is a copy of the template taken on the day
    you made it, and nothing can push an update into it — so an old
    contract is an ordinary state of affairs, not a fault. The value here
    is naming it, because the symptom otherwise arrives as some unrelated
    field being ignored.
    """
    from apps.brain.services import gitrepo

    probe = gitrepo.contract_version_probe()
    server, brain, state = probe["server"], probe["brain"], probe["state"]

    if state == "ok":
        return _check("contract_version", "ok", f"Brain contract {server}",
                      "Your brain declares the contract this server speaks.")
    if state == "unknown":
        return _check(
            "contract_version", "warn", "Your brain does not declare a contract version",
            (f"This server speaks contract {server}. The clone's CLAUDE.md has no "
             "readable `contract-version` in its frontmatter"
             + (f" (it reads {brain!r})" if brain else "")
             + ". Brains created before the field existed look like this. Nothing "
             "is broken; the server simply cannot tell you whether your contract "
             "has fallen behind."),
        )
    if state == "older":
        return _check(
            "contract_version", "warn", f"Your brain is on contract {brain}, this server speaks {server}",
            (f"Your brain was created from a {brain} template and never updated — "
             "which is normal, since nothing can push changes into your repo. "
             "Compare your CLAUDE.md against the current template and bring across "
             "what you want. The server keeps serving either way."),
        )
    return _check(
        "contract_version", "warn", f"Your brain is on contract {brain}, ahead of this server's {server}",
        (f"The clone declares {brain} but this server was built against {server}, so "
         "it may ignore parts of your contract it does not know about. This usually "
         "means the brain was updated and the server image was not — upgrade the "
         "server."),
    )


def check_ahead_of_github() -> dict:
    from apps.brain.services import gitcreds, gitrepo

    if not gitrepo.is_valid_repo():
        return _check("ahead", "ok", "Nothing pending", "No clone to compare.")
    ahead = gitrepo.local_only_commits()
    has_write = bool(gitcreds.status()["write"]["configured"])
    if ahead and not has_write:
        return _check(
            "ahead", "warn", f"Your brain is {ahead} commit(s) ahead of GitHub",
            "Approved feeds were committed here but never pushed, because no "
            "write credential is configured. They exist only on this server "
            "until you add one.",
        )
    if ahead:
        return _check(
            "ahead", "warn", f"Your brain is {ahead} commit(s) ahead of GitHub",
            "A push has not landed. Check the Tasks page for a failed approval.",
        )
    return _check("ahead", "ok", "In sync with GitHub", "Nothing unpushed.")


def check_write_credential() -> dict:
    from apps.brain.services import gitcreds

    status = gitcreds.status()["write"]
    if status["configured"]:
        return _check("write", "ok", "Write credential set",
                      f"Approvals push to GitHub (source: {status['source']}).")
    return _check(
        "write", "warn", "No write credential",
        "Approved feeds will commit on this server but never reach GitHub. "
        "Add a fine-grained PAT with Contents: read+write on the brain repo.",
        link_step="write",
    )


def check_webhook() -> dict:
    from apps.brainconfig import services as cfg

    if cfg.get("GITHUB_WEBHOOK_SECRET"):
        return _check("webhook", "ok", "Push webhook configured",
                      "Pushes to GitHub sync here within seconds.")
    return _check(
        "webhook", "warn", "No webhook — pushes sync within 15 minutes",
        "Without a webhook secret the server only notices your pushes on its "
        "periodic pull. Generate a secret, then add it to your repository's "
        "webhook settings.",
        action="generate_webhook_secret",
    )


def check_backup(request=None) -> dict:
    """Feeds, events, chat and the token ledger exist ONLY in the database."""
    if store.get_str(KEY_BACKUP_ACK, "") == "1":
        return _check("backup", "ok", "Database backup acknowledged",
                      "You've confirmed you have a backup strategy.")
    return _check(
        "backup", "warn", "No backup of the database",
        "Your notes are safe in git, but feeds, events, chat history and the "
        "token ledger are not in the repository and cannot be rebuilt from "
        "it. So is boot-secrets.json on the state volume — without it every "
        "stored credential becomes unreadable. Set up a periodic pg_dump plus "
        "a copy of that file, then dismiss this.",
        action="ack_backup",
    )


def check_admin_url() -> dict:
    """Where Django's own admin lives — otherwise a generated slug is lost."""
    path = (settings.DJANGO_ADMIN_URL_PATH or "").strip("/")
    if not path:
        return _check("admin_url", "info", "Django admin disabled",
                      "No DJANGO_ADMIN_URL_PATH is set.")
    return _check(
        "admin_url", "info", "Django admin path",
        f"/{path}/ — generated for this install, and not guessable. This is "
        "the only place it is shown; note it down if you want it.",
    )


def check_debug() -> dict:
    if not settings.DEBUG:
        return _check("debug", "ok", "Debug mode off", "Errors don't leak tracebacks.")
    return _check(
        "debug", "danger", "DEBUG is on",
        "Every error page exposes source, settings names and SQL. Set "
        "DEBUG=false for anything but local development.",
    )


# ---- aggregate -----------------------------------------------------------


def all_checks(request) -> list[dict]:
    """Every check, most severe first. A check that raises becomes a check."""
    runners = (
        ("exposure", lambda: check_exposure(request)),
        ("debug", check_debug),
        ("secrets", check_unreadable_secrets),
        ("clone", check_clone),
        ("contract_version", check_contract_version),
        ("ahead", check_ahead_of_github),
        ("write", check_write_credential),
        ("webhook", check_webhook),
        ("backup", check_backup),
        ("admin_url", check_admin_url),
    )
    results = []
    for name, fn in runners:
        try:
            results.append(fn())
        except Exception as exc:
            log.exception("health check %s failed", name)
            results.append(_check(
                name, "warn", f"Could not run the {name} check",
                f"{exc.__class__.__name__}: {exc}",
            ))
    order = {"danger": 0, "warn": 1, "info": 2, "ok": 3}
    results.sort(key=lambda c: order.get(c["level"], 9))
    return results


def problems(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c["level"] in ("danger", "warn")]


def acknowledge_backup() -> None:
    store.set_value(KEY_BACKUP_ACK, "1")
