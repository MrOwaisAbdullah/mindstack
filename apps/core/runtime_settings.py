"""Runtime-overridable settings.

Two operator-flippable knobs live here:

- **Admin IP allowlist** (`get_admin_ip_allowlist` /
  `set_admin_ip_allowlist`): comma-separated CIDR list gating the
  `/admin/` and `/admin_panel/` prefixes.
- **Audit retention days** (`get_audit_retention_days` /
  `set_audit_retention_days`): integer day count the audit-prune cron
  honors.

Persisted in Postgres via `apps.core.models.RuntimeSetting` with
Redis as a 5-minute read-through cache (see
`apps.core.runtime_setting_store`). The previous version cached the
override in Redis with `timeout=None` and fell through to the env
value on cache miss — a `down --volumes` or boot-time Redis blip
silently reverted the operator's choice. DB storage makes the
override durable; the cache is purely for read speed.

Public API stays unchanged so admin views, middleware, and the
audit-prune cron don't have to move.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.conf import settings

from apps.core import audit_hook, runtime_setting_store

log = logging.getLogger(__name__)

_KEY_IP_ALLOWLIST = "admin_ip_allowlist"
_KEY_AUDIT_RETENTION = "audit_retention_days"


# ---- IP allowlist -------------------------------------------------------


def get_admin_ip_allowlist() -> list[str]:
    """Returns the effective allowlist: DB override if set, else env."""
    raw = runtime_setting_store.get_str(_KEY_IP_ALLOWLIST, default="")
    if raw:
        return _split_cidrs(raw)
    env_raw = getattr(settings, "ADMIN_IP_ALLOWLIST", []) or []
    if isinstance(env_raw, str):
        return _split_cidrs(env_raw)
    return [str(s) for s in env_raw]


def set_admin_ip_allowlist(
    cidrs: Iterable[str],
    *,
    actor=None,
    request_id: str = "",
    ip: Optional[str] = None,
) -> None:
    """Persist a runtime override for the admin IP allowlist."""
    cleaned = sorted({c.strip() for c in cidrs if c and c.strip()})
    before = get_admin_ip_allowlist()
    runtime_setting_store.set_value(
        _KEY_IP_ALLOWLIST, ",".join(cleaned), actor=actor
    )
    audit_hook.record(
        action="settings.admin_ip_allowlist.updated",
        actor=actor,
        actor_label="" if actor else "system",
        target_type="setting",
        target_id="admin_ip_allowlist",
        before={"cidrs": before},
        after={"cidrs": cleaned},
        ip=ip,
        request_id=request_id,
        is_compliance=False,
    )


def clear_admin_ip_allowlist_override(
    *, actor=None, request_id: str = "", ip: Optional[str] = None
) -> None:
    """Drop the runtime override; subsequent reads fall through to env."""
    before = get_admin_ip_allowlist()
    runtime_setting_store.clear(_KEY_IP_ALLOWLIST)
    audit_hook.record(
        action="settings.admin_ip_allowlist.cleared",
        actor=actor,
        actor_label="" if actor else "system",
        target_type="setting",
        target_id="admin_ip_allowlist",
        before={"cidrs": before},
        after={"cidrs": list(getattr(settings, "ADMIN_IP_ALLOWLIST", []) or [])},
        ip=ip,
        request_id=request_id,
        is_compliance=False,
    )


def _split_cidrs(raw: str) -> list[str]:
    """Split a comma/newline-joined CIDR string into a list."""
    out: list[str] = []
    for chunk in raw.replace("\r", "\n").replace(",", "\n").split("\n"):
        c = chunk.strip()
        if c:
            out.append(c)
    return sorted(set(out))


# ---- Audit retention days ----------------------------------------------


def get_audit_retention_days() -> int:
    """Effective AUDIT_RETENTION_DAYS — DB override else env."""
    env_default = int(getattr(settings, "AUDIT_RETENTION_DAYS", 180) or 180)
    return runtime_setting_store.get_coerced(
        _KEY_AUDIT_RETENTION,
        default=env_default,
        coerce=lambda v: max(1, int(v)),
    )


def set_audit_retention_days(
    days: int,
    *,
    actor=None,
    request_id: str = "",
    ip: Optional[str] = None,
) -> None:
    """Persist a runtime override for AUDIT_RETENTION_DAYS."""
    days = max(1, int(days))
    before = get_audit_retention_days()
    runtime_setting_store.set_value(
        _KEY_AUDIT_RETENTION, str(days), actor=actor
    )
    audit_hook.record(
        action="settings.audit_retention_days.updated",
        actor=actor,
        actor_label="" if actor else "system",
        target_type="setting",
        target_id="audit_retention_days",
        before={"days": before},
        after={"days": days},
        ip=ip,
        request_id=request_id,
        is_compliance=False,
    )


__all__ = [
    "clear_admin_ip_allowlist_override",
    "get_admin_ip_allowlist",
    "get_audit_retention_days",
    "set_admin_ip_allowlist",
    "set_audit_retention_days",
]
