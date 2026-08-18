"""Maintenance-mode flag store.

Operators flip a flag in admin Settings that
puts the platform into a "down for maintenance" state: every non-staff
request gets a branded 503 response with a `Retry-After` header. Staff
keep working so they can finish whatever the maintenance window is for.

Storage: Postgres via `apps.core.models.RuntimeSetting`, with Redis
as a 5-minute read-through cache (see
`apps.core.runtime_setting_store`). Prior to this module's rewrite
the flag was Redis-only — a `down --volumes` would silently re-open
the site while the operator believed maintenance mode was still on.

Reads happen on every request (the middleware calls `is_enabled()`),
so this needs to be cheap. The cache hit is sub-ms; a cache miss
falls through to a single indexed PK lookup on `core_runtime_setting`.

Default-when-everything-down: `is_enabled()` returns False (site
stays open). The opposite default would mean a Redis + DB joint
incident silently locks users out — worse failure mode, since most
ops teams want maintenance mode to be an explicit operator action,
not "infra hiccupped".
"""
from __future__ import annotations

import logging
from typing import Optional

from apps.core import audit_hook, runtime_setting_store

log = logging.getLogger(__name__)

_KEY_ENABLED = "maintenance_enabled"
_KEY_MESSAGE = "maintenance_message"
_DEFAULT_MESSAGE = (
    "We're updating the platform — back online shortly. "
    "Thanks for your patience."
)


def is_enabled() -> bool:
    """Returns True iff the maintenance flag is set.

    The stored value is "1" or "0" (textual); any other value
    (empty, garbage from a hand-edited row) reads as False.
    """
    return runtime_setting_store.get_str(_KEY_ENABLED, default="0") == "1"


def get_message() -> str:
    """Returns the operator-supplied banner message, or the default."""
    return runtime_setting_store.get_str(_KEY_MESSAGE, default="") or _DEFAULT_MESSAGE


def set_enabled(
    enabled: bool,
    *,
    message: Optional[str] = None,
    actor=None,
    actor_label: str = "",
    request_id: str = "",
    ip: Optional[str] = None,
) -> None:
    """Flip maintenance mode on/off and emit an audit row.

    `message` updates the banner text on the maintenance page. Pass
    None to leave the existing message unchanged.

    Audit emission is best-effort via `apps.core.audit_hook` so this
    function is callable from apps.core without importing apps.audit
    (Contract 1).
    """
    before = {"enabled": is_enabled(), "message": get_message()}
    runtime_setting_store.set_value(
        _KEY_ENABLED, "1" if enabled else "0", actor=actor
    )
    if message is not None:
        runtime_setting_store.set_value(_KEY_MESSAGE, message, actor=actor)
    after = {
        "enabled": enabled,
        "message": message if message is not None else before["message"],
    }

    audit_hook.record(
        action="settings.maintenance_mode.toggled",
        actor=actor,
        actor_label=actor_label or ("system" if actor is None else ""),
        target_type="setting",
        target_id="maintenance_mode",
        before=before,
        after=after,
        ip=ip,
        request_id=request_id,
        is_compliance=False,
    )


def clear() -> None:
    """Test helper — drop both rows (DB + cache)."""
    runtime_setting_store.clear(_KEY_ENABLED)
    runtime_setting_store.clear(_KEY_MESSAGE)


__all__ = ["clear", "get_message", "is_enabled", "set_enabled"]
