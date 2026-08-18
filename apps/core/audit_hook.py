"""Audit-recording hook registry.

apps.core consumers (security middleware, decorator wrappers, future
admin views the framework exposes) need to write `AuditEvent` rows
without importing apps.audit (Contract 1: apps.core has no reverse
deps on feature apps). This module is the bridge: `AuditConfig.ready()`
registers a recorder at boot, apps.core dispatches without knowing
the implementation.

Same shape as `apps.core.bearer` / `apps.core.error_hook`. When no
recorder is registered, `record(...)` is a silent no-op so request
handling never breaks on an unwired recorder.

In this fork the recorder is `apps.events.sinks.record_audit`, installed
by `EventsConfig.ready()`; rows land in the `Event` log.

Recorder contract:

    recorder(
        *,
        action: str,                     # `<domain>.<entity>.<verb>`, e.g. `security.honeypot.hit`
        actor: AbstractBaseUser | None,
        actor_label: str,                # for system-driven rows
        target_type: str,
        target_id: str | int,
        before: dict | None,
        after: dict | None,
        ip: str | None,
        user_agent: str,
        request_id: str,
        is_compliance: bool,
    ) -> None
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

log = logging.getLogger(__name__)


class AuditRecorder(Protocol):
    def __call__(
        self,
        *,
        action: str,
        actor: "AbstractBaseUser | None" = None,
        actor_label: str = "",
        target_type: str = "",
        target_id: str | int = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str = "",
        request_id: str = "",
        is_compliance: bool = False,
    ) -> None: ...


_recorder: Optional[AuditRecorder] = None


def register(recorder: AuditRecorder) -> None:
    """Install the audit recorder. Called from `AuditConfig.ready()`."""
    global _recorder
    _recorder = recorder


def is_enabled() -> bool:
    return _recorder is not None


def record(
    *,
    action: str,
    actor: "AbstractBaseUser | None" = None,
    actor_label: str = "",
    target_type: str = "",
    target_id: str | int = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str = "",
    request_id: str = "",
    is_compliance: bool = False,
) -> None:
    """Write an `AuditEvent` row via the registered recorder. No-op when
    no recorder is wired."""
    if _recorder is None:
        return
    try:
        _recorder(
            action=action,
            actor=actor,
            actor_label=actor_label,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
            ip=ip,
            user_agent=user_agent,
            request_id=request_id,
            is_compliance=is_compliance,
        )
    except Exception:
        # Defensive — the audit recorder itself is best-effort
        # (apps.audit.services.record swallows DB errors), so this
        # should never fire. But the registry layer being defensive
        # too means an audit-recorder regression can't break a request.
        log.exception("apps.core.audit_hook.record: recorder raised", extra={"action": action})


def unregister() -> Callable[[], None]:
    """Test helper — drops the registered recorder; returns a closure
    that restores the previous one."""
    global _recorder
    previous = _recorder
    _recorder = None

    def restore() -> None:
        global _recorder
        _recorder = previous

    return restore


__all__ = ["AuditRecorder", "is_enabled", "record", "register", "unregister"]
