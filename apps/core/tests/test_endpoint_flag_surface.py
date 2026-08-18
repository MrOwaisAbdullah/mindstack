"""The endpoint kill switch is reachable from the ops UI.

`endpoint_gating` had a DB model, a Redis cache, an audit call, and
enforcement in both the REST view and the MCP proxy's `tools/list` —
and `set_disabled` had zero non-test callers. The only way to take a
buggy endpoint offline was a Django shell, which the errors guide
actually prescribed. Same unreachable-from-one-end shape maintenance
mode had; the surface lands next to it on `/ops/settings/`.

The GET assertions here are deliberately structural (the action value,
the slug list): an inert card would pass any behavioural test that only
POSTs, because the POST path doesn't go through the template.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.core import endpoint_gating
from apps.core.models import EndpointFlag
from apps.core.registry import registry


@pytest.fixture(autouse=True)
def configured_install(monkeypatch):
    """Get `SetupRequiredMiddleware` out of the way (same seam as the
    maintenance-mode tests — it 302s every ops route to `/setup/` until
    a real clone exists on disk)."""
    from apps.brainconfig import setup_state

    monkeypatch.setattr(setup_state, "needs_first_admin", lambda: False)
    monkeypatch.setattr(setup_state, "is_complete", lambda: True)


@pytest.fixture(autouse=True)
def clean_gate():
    """Locmem cache keys survive across tests in one process."""
    endpoint_gating.clear_cache()
    yield
    endpoint_gating.clear_cache()


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(
        "endpoint-op", password="x" * 20, is_staff=True, is_superuser=True
    )
    client.force_login(user)
    return client


def test_settings_page_lists_every_registered_endpoint(staff_client) -> None:
    html = staff_client.get("/ops/settings/").content.decode()
    slugs = {spec.slug for spec in registry.all()}
    assert slugs, "registry is empty — the app configs did not load endpoints"
    for slug in slugs:
        assert slug in html, f"{slug} is missing from the Endpoints card"
    # The control itself, not just the listing: the POST handler is only
    # reachable if the rendered form carries the action.
    assert 'value="endpoint"' in html


@pytest.mark.django_db
def test_disable_gates_the_rest_endpoint_and_enable_restores_it(staff_client) -> None:
    try:
        staff_client.post(
            "/ops/settings/",
            {"action": "endpoint", "slug": "ping", "disabled": "1", "reason": "broken"},
        )
        flag = EndpointFlag.objects.get(slug="ping")
        assert flag.disabled is True
        assert flag.reason == "broken"

        # The gate sits before auth, so disabled beats the 401 an
        # unauthenticated call would otherwise get.
        response = staff_client.post(
            "/api/v1/ping", data="{}", content_type="application/json"
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "endpoint_disabled"

        staff_client.post(
            "/ops/settings/", {"action": "endpoint", "slug": "ping", "disabled": "0"}
        )
        assert endpoint_gating.is_disabled("ping") is False
        response = staff_client.post(
            "/api/v1/ping", data="{}", content_type="application/json"
        )
        assert response.status_code != 503
    finally:
        EndpointFlag.objects.filter(slug="ping").delete()


@pytest.mark.django_db
def test_the_disabled_state_shows_on_the_page(staff_client) -> None:
    try:
        staff_client.post(
            "/ops/settings/",
            {"action": "endpoint", "slug": "ping", "disabled": "1", "reason": "smoke"},
        )
        html = staff_client.get("/ops/settings/").content.decode()
        assert "smoke" in html
        assert 'name="disabled" value="0"' in html, "no Enable control for the disabled row"
    finally:
        EndpointFlag.objects.filter(slug="ping").delete()


@pytest.mark.django_db
def test_unknown_slug_mints_no_row(staff_client) -> None:
    staff_client.post(
        "/ops/settings/",
        {"action": "endpoint", "slug": "not-an-endpoint", "disabled": "1"},
    )
    assert not EndpointFlag.objects.filter(slug="not-an-endpoint").exists()


@pytest.mark.django_db
def test_orphaned_flag_is_listed_and_can_be_switched_off(staff_client) -> None:
    """A flag whose endpoint was renamed away must stay visible — it
    gates nothing today but re-arms if the slug ever returns."""
    try:
        endpoint_gating.set_disabled("retired-endpoint", True, reason="old")
        html = staff_client.get("/ops/settings/").content.decode()
        assert "retired-endpoint" in html

        staff_client.post(
            "/ops/settings/",
            {"action": "endpoint", "slug": "retired-endpoint", "disabled": "0"},
        )
        assert EndpointFlag.objects.get(slug="retired-endpoint").disabled is False
    finally:
        EndpointFlag.objects.filter(slug="retired-endpoint").delete()


@pytest.mark.django_db
def test_toggling_from_the_page_leaves_an_attributed_audit_row(staff_client) -> None:
    """`set_disabled` audits on its own (covered elsewhere); what the view
    adds is the actor — a shell toggle has none, a UI toggle must."""
    from apps.events.models import Event

    try:
        Event.objects.all().delete()
        staff_client.post(
            "/ops/settings/",
            {"action": "endpoint", "slug": "ping", "disabled": "1", "reason": "audit me"},
        )
        row = Event.objects.get(type="settings_change")
        assert row.details["action"] == "settings.endpoint.toggled"
        assert row.details["target_id"] == "ping"
        assert row.details["after"]["disabled"] is True
        assert row.details["actor_id"] == User.objects.get(username="endpoint-op").pk
    finally:
        EndpointFlag.objects.filter(slug="ping").delete()
        Event.objects.all().delete()
