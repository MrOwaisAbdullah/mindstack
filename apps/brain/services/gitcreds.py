"""Where the two git credentials come from (SETUP-DESIGN.md §Credentials).

The brain needs a **read** credential (clone + every routine pull) and a
**write** credential (the approval handler's push). Until now both were
files an operator had to mount before first boot, which is most of what
made setup a terminal job.

Both now resolve in this order, and the order is the whole design:

    1. an env var                  — infrastructure-as-code wins
    2. a file path from an env var — compose/k8s secrets keep working
    3. an encrypted AppSetting     — what the setup wizard writes

So an operator who wants the credentials nowhere near the database keeps
exactly the setup they have today, and a newcomer never sees a file path.

**The security tradeoff is real and is recorded honestly here as well as
in SECURITY.md.** Previously the write PAT was mounted only into the
worker container, so the internet-facing web container physically could
not read it (PLAN.md grill C13). With the PAT in the database, any
container with DB access can. "Django RCE → read PAT → rewrite the
brain's history" becomes reachable where it wasn't. Accepted for a
single-user self-hosted product because the alternative is a setup nobody
completes — and mitigated by: the env/file override above (the split
stays available to anyone who wants it), and the code-level boundary that
only the worker's approval handler ever calls `write_pat()`.

Unrelated to prompt injection: the reader agent is confined to tier
snapshots with `Read` allow-listed to that one directory and no Bash, so
it cannot reach the materialised key file either way.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from django.conf import settings as dj_settings

log = logging.getLogger(__name__)

#: Encrypted AppSetting keys. Deliberately NOT in `brainconfig.REGISTRY`:
#: the private key is never typed by a human and never rendered, so it has
#: no business being a row on a settings form.
KEY_SSH_PRIVATE = "BRAIN_GIT_SSH_PRIVATE_KEY"
KEY_SSH_PUBLIC = "BRAIN_GIT_SSH_PUBLIC_KEY"
KEY_WRITE_PAT = "BRAIN_GIT_WRITE_PAT"

#: Comment appended to the generated public key, so the entry is
#: identifiable in GitHub's deploy-key list a year from now.
KEY_COMMENT = "brainoutside"


def _db_get(key: str) -> str:
    """Read an encrypted setting, tolerating an unreachable database.

    `_git_env()` runs on every git subprocess, including during boot and
    inside health probes. A DB blip should degrade to "no configured
    credential", not raise out of a health check.
    """
    try:
        from apps.brainconfig import services as cfg

        return cfg.get(key)
    except Exception:  # pragma: no cover - DB down / apps not ready
        log.warning("gitcreds: could not read %s from settings", key, exc_info=True)
        return ""


def _db_set(key: str, value: str, *, actor=None) -> None:
    from apps.brainconfig import services as cfg

    cfg.set_value(key, value, actor=actor)


def _file_contents(path: str) -> str:
    p = Path(path.strip()) if path and path.strip() else None
    if p is None or not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:  # pragma: no cover - unreadable mount
        log.warning("gitcreds: %s exists but could not be read", p, exc_info=True)
        return ""


# ---- read credential: the SSH deploy key --------------------------------


def generate_keypair(*, actor=None) -> str:
    """Mint a fresh ed25519 deploy key. Returns the PUBLIC half.

    Overwrites any existing pair — the caller decides whether that is a
    first-run generate or a deliberate rotation. After rotation the old
    public key in GitHub is dead, so the UI must say so.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    public = f"{public} {KEY_COMMENT}"

    _db_set(KEY_SSH_PRIVATE, private, actor=actor)
    _db_set(KEY_SSH_PUBLIC, public, actor=actor)
    log.info("gitcreds: generated a new ed25519 deploy keypair")
    return public


def ensure_keypair(*, actor=None) -> str:
    """Public half of the deploy key, generating the pair if absent."""
    existing = _db_get(KEY_SSH_PUBLIC)
    if existing and _db_get(KEY_SSH_PRIVATE):
        return existing
    return generate_keypair(actor=actor)


def public_key() -> str:
    """Public half if we hold one, else "". Never generates."""
    return _db_get(KEY_SSH_PUBLIC)


def _materialise(private_key: str) -> str:
    """Write the private key to a 0600 file and return its path.

    ssh needs a file; the key lives in the database. The file is written
    into the container's temp dir (never a mounted volume, never the
    clone) and rewritten only when the key itself changes.
    """
    if not private_key.endswith("\n"):
        private_key += "\n"  # ssh rejects a key without a trailing newline
    fingerprint = hashlib.sha256(private_key.encode()).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "brainoutside-git"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    target = d / f"id_{fingerprint}"
    if not target.exists():
        # Write-then-rename so a concurrent reader never sees a partial
        # key, and so two threads racing produce the same final file.
        tmp = d / f".id_{fingerprint}.{os.getpid()}"
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(private_key)
        os.replace(str(tmp), str(target))
    return str(target)


def ssh_key_path() -> str:
    """Path to the private key git should use, or "" for ambient creds.

    An env-configured path that is missing or EMPTY counts as unset: the
    local stack mounts a zero-byte placeholder at
    `/run/secrets/brain_deploy_key` so the bind mount doesn't become a
    directory, and treating that as a real key would hand ssh
    `-o IdentitiesOnly=yes` with nothing to identify with.
    """
    env_path = (dj_settings.BRAIN_GIT_SSH_KEY_PATH or "").strip()
    if env_path and _file_contents(env_path):
        return env_path
    private = _db_get(KEY_SSH_PRIVATE)
    if private:
        return _materialise(private)
    return ""


# ---- write credential: the fine-grained PAT ------------------------------


def write_pat() -> str:
    """The push credential. Only the worker's approval handler calls this."""
    env_value = dj_settings.BRAIN_GIT_WRITE_PAT.get_secret_value().strip()
    if env_value:
        return env_value
    from_file = _file_contents(dj_settings.BRAIN_GIT_WRITE_PAT_PATH)
    if from_file:
        return from_file
    return _db_get(KEY_WRITE_PAT)


def set_write_pat(pat: str, *, actor=None) -> None:
    _db_set(KEY_WRITE_PAT, pat.strip(), actor=actor)


# ---- introspection for the wizard / health panel -------------------------


def _source_of(env_value: str, env_path: str, db_value: str) -> str:
    if env_value:
        return "env"
    if env_path and _file_contents(env_path):
        return "file"
    if db_value:
        return "settings"
    return "unset"


def status() -> dict[str, object]:
    """Which credentials exist and where each one came from.

    Values are never included — only presence and provenance.
    """
    env_key_path = (dj_settings.BRAIN_GIT_SSH_KEY_PATH or "").strip()
    priv = _db_get(KEY_SSH_PRIVATE)
    pat_env = dj_settings.BRAIN_GIT_WRITE_PAT.get_secret_value().strip()
    pat_path = dj_settings.BRAIN_GIT_WRITE_PAT_PATH
    read_source = _source_of("", env_key_path, priv)
    pat_source = _source_of(pat_env, pat_path, _db_get(KEY_WRITE_PAT))
    return {
        "read": {
            "configured": read_source != "unset",
            "source": read_source,
            "public_key": _db_get(KEY_SSH_PUBLIC) if read_source == "settings" else "",
        },
        "write": {
            "configured": pat_source != "unset",
            "source": pat_source,
        },
    }


__all__ = [
    "KEY_SSH_PRIVATE",
    "KEY_SSH_PUBLIC",
    "KEY_WRITE_PAT",
    "ensure_keypair",
    "generate_keypair",
    "public_key",
    "set_write_pat",
    "ssh_key_path",
    "status",
    "write_pat",
]
