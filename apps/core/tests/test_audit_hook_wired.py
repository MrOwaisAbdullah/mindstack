"""`audit_hook` reaches a real sink, so config changes leave a trail.

Seven call sites across the framework describe themselves as writing an
audit row: endpoint disable, maintenance mode, the admin IP allowlist,
audit retention, and honeypot hits. None of them wrote anything —
`apps.audit` was never vendored, so nothing called `register()` and
`record()` returned on `if _recorder is None`.

The failure mode this pins is quiet. An audit call that records nothing
looks identical, from the caller's side, to one that records: the
function returns None either way, and `audit_hook.record` swallows
recorder exceptions on purpose. Only asking the database proves it.
"""
from __future__ import annotations

import pytest

from apps.core import audit_hook, endpoint_gating, maintenance, runtime_settings
from apps.events import sinks
from apps.events.models import Event


def test_boot_registers_an_audit_recorder() -> None:
    assert audit_hook.is_enabled(), (
        "No audit recorder is registered — every config change goes "
        "unrecorded. See apps/events/apps.py."
    )


@pytest.mark.django_db
def test_record_writes_an_event_row() -> None:
    audit_hook.record(
        action="settings.maintenance_mode.toggled",
        actor_label="system",
        target_type="setting",
        target_id="maintenance_mode",
        before={"enabled": False},
        after={"enabled": True},
        ip="203.0.113.4",
        request_id="req-9",
    )

    event = Event.objects.get()
    assert event.type == "settings_change"
    assert event.details["audit"] is True
    assert event.details["action"] == "settings.maintenance_mode.toggled"
    assert event.details["before"] == {"enabled": False}
    assert event.details["after"] == {"enabled": True}
    assert event.details["actor"] == "system"
    assert event.details["request_id"] == "req-9"


@pytest.mark.django_db
def test_security_actions_land_next_to_other_denials() -> None:
    """A honeypot hit belongs with the webhook-HMAC rejections
    (`auth_denied`), not in the settings stream."""
    audit_hook.record(
        action="security.honeypot.hit",
        actor_label="system:honeypot",
        target_type="path",
        target_id="/wp-admin/",
        ip="198.51.100.7",
    )
    assert Event.objects.get().type == "auth_denied"


@pytest.mark.django_db
def test_unmapped_domain_still_records() -> None:
    """Losing an audit event to an unrecognised prefix is the one outcome
    an audit trail cannot have."""
    audit_hook.record(action="brandnew.thing.happened", target_id="x")

    event = Event.objects.get()
    assert event.type == sinks._FALLBACK_EVENT_TYPE
    assert event.details["action"] == "brandnew.thing.happened"


@pytest.mark.django_db
def test_actor_is_resolved_to_something_readable() -> None:
    from django.contrib.auth.models import User

    user = User.objects.create_user("op", email="op@example.test")
    audit_hook.record(action="settings.endpoint.toggled", actor=user, target_id="get-note")

    details = Event.objects.get().details
    assert details["actor"] == "op@example.test"
    assert details["actor_id"] == user.pk


def test_recorder_swallows_its_own_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """No django_db mark: the write fails, and the caller — a config
    writer that has already committed — must not see an exception."""

    class _Boom:
        objects = None

    import apps.events.models as models_module

    monkeypatch.setattr(models_module, "Event", _Boom)
    sinks.record_audit(action="settings.endpoint.toggled", target_id="x")


# ---- the real call sites, end to end ---------------------------------------


@pytest.mark.django_db
def test_endpoint_disable_leaves_a_trail() -> None:
    changed = endpoint_gating.set_disabled("get-note", True, reason="broken")
    assert changed

    details = Event.objects.get(type="settings_change").details
    assert details["action"] == "settings.endpoint.toggled"
    assert details["target_id"] == "get-note"
    assert details["after"] == {"disabled": True, "reason": "broken"}


@pytest.mark.django_db
def test_maintenance_toggle_leaves_a_trail() -> None:
    maintenance.set_enabled(True, message="back shortly")
    try:
        details = Event.objects.get(type="settings_change").details
        assert details["action"] == "settings.maintenance_mode.toggled"
        assert details["after"]["enabled"] is True
        assert details["after"]["message"] == "back shortly"
    finally:
        maintenance.clear()


@pytest.mark.django_db
def test_admin_ip_allowlist_change_leaves_a_trail() -> None:
    runtime_settings.set_admin_ip_allowlist(["10.0.0.0/8", "192.0.2.1/32"])

    details = Event.objects.get(type="settings_change").details
    assert details["action"] == "settings.admin_ip_allowlist.updated"
    assert details["after"] == {"cidrs": ["10.0.0.0/8", "192.0.2.1/32"]}
