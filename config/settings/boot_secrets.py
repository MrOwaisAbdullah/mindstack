"""Generate-once, persist-forever boot secrets (SETUP-DESIGN.md, build order #1).

Four values must exist before Django can serve a single request, and all
four are read at *import* time — so none of them can live in the database
(`SECRET_KEY` signs the session that logs you in; `FIELD_ENCRYPTION_KEY`
decrypts the settings table itself; the admin slug is baked into
`config/urls.py`). Demanding that a newcomer generate them by hand is the
single biggest reason the current install needs a terminal.

So the app generates them on first boot and writes them to a file on a
**persisted** volume. Required env then shrinks to `POSTGRES_PASSWORD`
plus the domain.

Hard requirement, and the reason this module is more careful than it
looks: a `FIELD_ENCRYPTION_KEY` that regenerates on the next boot makes
every stored credential (the Claude key, the git PAT) silently
undecryptable — `AppSetting.value` swallows `InvalidToken` and returns
"", so the failure reads as "my settings were wiped", not "the key
changed". Two consequences:

- **Never silently regenerate.** If the state directory cannot be written,
  boot FAILS with an explanation rather than continuing on an ephemeral
  key that will vanish.
- **Never race.** web, mcp and worker boot simultaneously off the same
  volume. Generation happens under the same cross-container file lock the
  git layer uses, and the writer re-reads the file inside the lock, so
  every container ends up with byte-identical values.

Explicit env always wins: a value supplied by the operator (env var,
compose, `.env`) is used as-is and nothing is generated for it. This
module only fills in what is absent or still on a shipped placeholder.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

#: Bumped only if the on-disk shape changes incompatibly.
STATE_VERSION = 1

STATE_FILENAME = "boot-secrets.json"
LOCK_FILENAME = "boot-secrets.lock"

#: Generous, because this blocks boot. Contention is three containers
#: starting at once, so the real wait is milliseconds; a 60s ceiling only
#: matters if a lock file is stale, and failing loudly there is correct.
LOCK_TIMEOUT_SECONDS = 60


class BootSecretsError(RuntimeError):
    """Raised when boot secrets can neither be read nor durably created."""


# --------------------------------------------------------------------------
# What we manage, and how to tell a real value from a shipped placeholder.
# --------------------------------------------------------------------------


def _gen_secret_key() -> str:
    return secrets.token_urlsafe(64)


def _gen_fernet_key() -> str:
    # Imported lazily: cryptography is a heavy import for a settings module
    # and is only needed the one time a key is actually generated.
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _gen_loopback_secret() -> str:
    return secrets.token_urlsafe(32)


def _gen_admin_slug() -> str:
    # Not guessable, and visibly not a default. Bots scan `/admin/`.
    return f"_admin-{secrets.token_hex(8)}/"


def _placeholder_secret_key(v: str) -> bool:
    return not v.strip() or v.strip() == "change-me-dev-only-not-for-prod"


def _blank(v: str) -> bool:
    return not v.strip()


def _placeholder_admin_slug(v: str) -> bool:
    # Only the shipped placeholder is replaced. A deliberate (if unwise)
    # value like "admin/" is left alone — `assert_prod_safe()` already
    # refuses to boot prod on it, with a message that says why.
    return not v.strip() or "CHANGE-ME" in v


#: field name -> (generator, is-placeholder predicate)
MANAGED: dict[str, tuple[Callable[[], str], Callable[[str], bool]]] = {
    "SECRET_KEY": (_gen_secret_key, _placeholder_secret_key),
    "FIELD_ENCRYPTION_KEY": (_gen_fernet_key, _blank),
    "MCP_LOOPBACK_SECRET": (_gen_loopback_secret, _blank),
    "DJANGO_ADMIN_URL_PATH": (_gen_admin_slug, _placeholder_admin_slug),
}

#: Managed fields that are `SecretStr` on the Settings model — assigning a
#: plain str to them would change the type callers see (`.get_secret_value()`
#: would blow up), so they get wrapped on the way back in.
_SECRET_STR_FIELDS = frozenset({"SECRET_KEY", "FIELD_ENCRYPTION_KEY", "MCP_LOOPBACK_SECRET"})


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def state_dir(raw: str, base_dir: Path) -> Path:
    """Resolve the state directory. Empty env → `<repo>/data/state`."""
    raw = (raw or "").strip()
    return Path(raw).resolve() if raw else (base_dir / "data" / "state")


def _read_state(path: Path) -> dict[str, str]:
    """Return the persisted secrets, or {} when the file is absent/unusable.

    A corrupt file is treated as absent *for reading* but never silently
    overwritten — `_write_state` refuses to drop keys it can't account for.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BootSecretsError(f"Cannot read {path}: {exc}") from exc
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootSecretsError(
            f"{path} exists but is not valid JSON ({exc}). It holds this "
            "install's SECRET_KEY and FIELD_ENCRYPTION_KEY — do not delete "
            "it blindly: restore it from a backup, or accept that every "
            "stored credential (Claude key, git PAT) must be re-entered."
        ) from exc
    values = doc.get("secrets")
    if not isinstance(values, dict):
        raise BootSecretsError(f"{path} has no `secrets` object — refusing to guess.")
    return {k: v for k, v in values.items() if isinstance(v, str) and v.strip()}


def _write_state(path: Path, values: dict[str, str]) -> None:
    """Persist atomically, owner-readable only. Caller holds the lock."""
    doc = {
        "version": STATE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_comment": (
            "Auto-generated by BrainOutside on first boot. Back this up with "
            "your database: losing FIELD_ENCRYPTION_KEY makes every stored "
            "credential unreadable, and losing SECRET_KEY logs everyone out. "
            "Any value set in the environment overrides the value here."
        ),
        "secrets": values,
    }
    tmp = path.with_name(path.name + ".tmp")
    # 0o600 before any content lands, so the secrets are never briefly
    # world-readable on a shared host.
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(str(tmp), str(path))


def _ensure_dir(d: Path) -> None:
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootSecretsError(
            f"Cannot create the state directory {d}: {exc}. This is where "
            "generated boot secrets are persisted; without it they would be "
            "regenerated on every restart, which makes every stored "
            "credential unreadable. Either mount a writable volume there "
            "(BRAIN_STATE_DIR), or set SECRET_KEY, FIELD_ENCRYPTION_KEY, "
            "MCP_LOOPBACK_SECRET and DJANGO_ADMIN_URL_PATH yourself."
        ) from exc
    if not os.access(str(d), os.W_OK):
        # `getuid` is POSIX-only; the app runs in Linux containers but the
        # settings module is also imported by host-run tooling on Windows.
        who = getattr(os, "getuid", lambda: "?")()
        raise BootSecretsError(
            f"The state directory {d} is not writable by uid {who} "
            "(the container runs as a non-root user). Generated boot secrets "
            "could not be persisted, and regenerating them on the next boot "
            "would make every stored credential unreadable. Fix the volume "
            "permissions, or set the four secrets in the environment."
        )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def apply_generated_secrets(settings_obj, *, base_dir: Path) -> list[str]:
    """Fill in any managed secret the operator didn't supply.

    Mutates `settings_obj` in place. Returns the names of the fields that
    were served from the persisted state file (generated now or earlier) —
    the caller may log them; the *values* are never logged.
    """
    needed = [
        name
        for name, (_gen, is_placeholder) in MANAGED.items()
        if is_placeholder(_as_text(getattr(settings_obj, name, "")))
    ]
    if not needed:
        return []

    d = state_dir(getattr(settings_obj, "BRAIN_STATE_DIR", ""), base_dir)
    _ensure_dir(d)
    path = d / STATE_FILENAME

    stored = _read_state(path)
    missing = [n for n in needed if n not in stored]

    if missing:
        # Generation is the only path that writes, so it is the only path
        # that needs the lock. web/mcp/worker start together off one volume.
        from filelock import FileLock, Timeout

        lock = FileLock(str(d / LOCK_FILENAME))
        try:
            with lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
                # Re-read INSIDE the lock: a sibling container may have
                # generated between our read and our acquire. Whoever wins
                # the lock first wins the values; the rest adopt them.
                stored = _read_state(path)
                still_missing = [n for n in needed if n not in stored]
                if still_missing:
                    for name in still_missing:
                        stored[name] = MANAGED[name][0]()
                    _write_state(path, stored)
                    log.warning(
                        "boot: generated and persisted %s at %s — back this "
                        "file up with your database",
                        ", ".join(sorted(still_missing)),
                        path,
                    )
        except Timeout as exc:
            raise BootSecretsError(
                f"Could not acquire {d / LOCK_FILENAME} within "
                f"{LOCK_TIMEOUT_SECONDS}s. Refusing to generate boot secrets "
                "unsynchronised — two containers with different keys would "
                "corrupt every encrypted setting. Remove the stale lock file "
                "if no other container is starting."
            ) from exc

    applied: list[str] = []
    for name in needed:
        value = stored.get(name)
        if not value:
            raise BootSecretsError(
                f"{name} is unset and was not persisted to {path}. This "
                "should be unreachable — report it."
            )
        _assign(settings_obj, name, value)
        applied.append(name)
    return applied


def _as_text(value) -> str:
    """Flatten `SecretStr | None | str` to a plain string for inspection."""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else str(value)


def _assign(settings_obj, name: str, value: str) -> None:
    if name in _SECRET_STR_FIELDS:
        from pydantic import SecretStr

        setattr(settings_obj, name, SecretStr(value))
    else:
        setattr(settings_obj, name, value)


__all__ = [
    "BootSecretsError",
    "MANAGED",
    "STATE_FILENAME",
    "apply_generated_secrets",
    "state_dir",
]
