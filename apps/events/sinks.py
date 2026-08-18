"""Concrete backends for the `apps.core` hook registries.

`apps.core` is the vendored framework half of this app and must not
import a feature app (Contract 1), so it dispatches through registries —
`apps.core.error_hook`, `apps.core.audit_hook` — that some feature app is
expected to fill in at boot. Upstream that app was `apps.observability`.
It was never vendored, nothing ever called `register()`, and so roughly a
dozen call sites across the request pipeline have been silent no-ops for
the life of the project: endpoint 500s recorded nothing, and
`/_csp-report/` accepted browser violation reports and dropped them.

This module is the missing half, pointed at the table this product
already has. `EventsConfig.ready()` registers it.

Same story for `apps.core.audit_hook`, whose seven call sites cover
every runtime configuration change the framework can make — endpoint
disable, maintenance mode, the admin IP allowlist, audit retention — plus
honeypot hits. All of them wrote nothing, so an install had no record of
who changed what, or when.

**Why `Event` and not a new `ErrorLog` model.** The hook's docstrings
describe an `ErrorLog` row with its own admin panel. That model does not
exist here and adding it would mean a second table, a second retention
cron, and a second ops page — for a single-operator server whose event
log is already the thing `/ops/logs/` renders, already filterable by
type, and already pruned by `run_prune_event_log`. The row lands as
`Event(type="error")` and shows up next to reads, syncs and feeds on the
timeline the operator already watches.

**What is deliberately not stored.** No traceback. `log.exception` has
already written one to the application log at every call site, the row
carries the same `request_id`, and a JSONField of tracebacks in a table
that also feeds a polling dashboard is a size problem for no gain. An
exception raised while serving a note can also quote note content, and
this table is backed up (PLAN.md §10) — the message is truncated for
that reason too, not only for size.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Truncation limits. `details` is JSON in Postgres, so nothing here is a
# hard constraint — these keep one pathological row from dominating a
# backup, and keep `/ops/logs/` (which slices details_json to 300 chars
# for display) from paging in far more than it renders.
_MAX_MESSAGE = 500
_MAX_PATH = 300
_MAX_USER_AGENT = 200
_MAX_SLUG = 64


def _clip(value: object, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def record_error(
    *,
    exc: BaseException,
    request_id: str = "",
    source: str = "",
    endpoint_slug: str = "",
    request_path: str = "",
    request_method: str = "",
    status_code: int = 500,
    user_id: int | None = None,
    ip: str | None = None,
    user_agent: str = "",
    handled: bool = False,
) -> str | None:
    """Persist one error as `Event(type="error")`. Returns the row's pk as
    a string, or None if the write failed.

    Fulfils `apps.core.error_hook.ErrorRecorder`. Callers treat this as
    best-effort — a failure to record must never turn a handled error into
    an unhandled one — so every exception is swallowed and logged here
    rather than propagating to the request path.

    `request_path` arrives pre-scrubbed on the paths that can carry a
    secret: `apps.core.rest` passes `request._scrubbed_path`, which
    `URLTokenScrubMiddleware` set with the `/mcp/k/<token>/` segment
    replaced. We do not re-scrub, because doing so here would imply the
    other call sites are safe to leave unscrubbed.
    """
    try:
        from apps.events.models import Event

        event = Event.objects.create(
            type="error",
            details={
                "exc_class": type(exc).__name__,
                "message": _clip(exc, _MAX_MESSAGE),
                # "rest" | "mcp" | "system" — which pipeline raised.
                "source": source,
                "endpoint_slug": _clip(endpoint_slug, _MAX_SLUG),
                "path": _clip(request_path, _MAX_PATH),
                "method": request_method,
                "status": status_code,
                # True: the endpoint caught this and still returned a real
                # response (`ctx.trace.exception`), or it is a CSP report.
                # False: the request died. The distinction is the first
                # thing an operator triages by.
                "handled": bool(handled),
                "request_id": request_id,
                "ip": ip or "",
                "user_agent": _clip(user_agent, _MAX_USER_AGENT),
                "user_id": user_id,
            },
        )
        return str(event.pk)
    except Exception:
        # Includes the DB being the thing that broke — which is exactly
        # when a 500 handler calls in here.
        log.exception(
            "events.sinks.record_error: could not persist error event",
            extra={"request_id": request_id, "endpoint_slug": endpoint_slug},
        )
        return None


# ---- audit -----------------------------------------------------------------

# Audit actions are namespaced `<domain>.<entity>.<verb>`. The domain
# decides which existing event type the row joins, so an operator reading
# `/ops/logs/` finds config changes in the same stream as the config
# changes `apps.brainconfig` already emits, and scanner probes in the same
# stream as the webhook-HMAC rejections. Adding an `audit` type instead
# would have split "who changed what" across two filters.
_ACTION_DOMAIN_TO_EVENT_TYPE = {
    # settings.endpoint.toggled, settings.maintenance_mode.toggled,
    # settings.admin_ip_allowlist.updated / .cleared,
    # settings.audit_retention_days.updated
    "settings": "settings_change",
    # security.honeypot.hit
    "security": "auth_denied",
}

# An action whose domain is not mapped above. It still gets a row —
# losing an audit event to a typo in a prefix is the one outcome an audit
# trail cannot have — but under the type that says "something happened
# that this sink did not anticipate".
_FALLBACK_EVENT_TYPE = "settings_change"

_MAX_ACTION = 128
_MAX_TARGET_ID = 200
_MAX_ACTOR_LABEL = 120


def _actor_label_for(actor: object, fallback: str) -> str:
    """A readable actor string. Single-operator product, so this is
    almost always the one account; it still matters for telling an
    operator action apart from a system flip (a management command, a
    scheduled task) after the fact."""
    if actor is None:
        return _clip(fallback or "system", _MAX_ACTOR_LABEL)
    for attribute in ("email", "username"):
        value = getattr(actor, attribute, "")
        if value:
            return _clip(value, _MAX_ACTOR_LABEL)
    return _clip(fallback or str(actor), _MAX_ACTOR_LABEL)


def record_audit(
    *,
    action: str,
    actor: object = None,
    actor_label: str = "",
    target_type: str = "",
    target_id: str | int = "",
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
    user_agent: str = "",
    request_id: str = "",
    is_compliance: bool = False,
) -> None:
    """Persist one audit row as an `Event`. Returns nothing — the hook's
    contract is fire-and-forget.

    Fulfils `apps.core.audit_hook.AuditRecorder`. `audit_hook.record`
    already wraps this in its own try/except, but this swallows too: a
    config write must not fail because the row describing it could not be
    written.

    `before` / `after` are stored verbatim. Every caller today diffs a
    `RuntimeSetting` or an `EndpointFlag`, both of which are plaintext
    tables by construction — secrets live in `AppSetting.value_encrypted`,
    on the other side of `apps.brainconfig.crypto`, and that writer
    deliberately logs only the key name. A future caller passing a secret
    through `before`/`after` would put it in a backed-up table in the
    clear; don't.
    """
    domain = (action or "").split(".", 1)[0]
    event_type = _ACTION_DOMAIN_TO_EVENT_TYPE.get(domain, _FALLBACK_EVENT_TYPE)
    if domain not in _ACTION_DOMAIN_TO_EVENT_TYPE:
        log.warning(
            "events.sinks.record_audit: unmapped audit domain %r "
            "(action=%r) — filed under %r",
            domain,
            action,
            _FALLBACK_EVENT_TYPE,
        )

    try:
        from apps.events.models import Event

        Event.objects.create(
            type=event_type,
            details={
                "audit": True,
                "action": _clip(action, _MAX_ACTION),
                "actor": _actor_label_for(actor, actor_label),
                "actor_id": getattr(actor, "pk", None),
                "target_type": target_type,
                "target_id": _clip(target_id, _MAX_TARGET_ID),
                "before": before,
                "after": after,
                "ip": ip or "",
                "user_agent": _clip(user_agent, _MAX_USER_AGENT),
                "request_id": request_id,
                "is_compliance": bool(is_compliance),
            },
        )
    except Exception:
        log.exception(
            "events.sinks.record_audit: could not persist audit event",
            extra={"action": action},
        )


__all__ = ["record_audit", "record_error"]
