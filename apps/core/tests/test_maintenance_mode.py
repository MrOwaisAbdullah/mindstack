"""Maintenance mode is enforced, and can be switched back off.

`MaintenanceModeMiddleware` was written, exported from
`apps.core.security`, given a DB-backed flag store with a Redis cache, a
branded 503 template and an audit call — and left out of `MIDDLEWARE`.
Nothing enforced it. There was also no surface anywhere that could set
the flag, so the feature was unreachable from both ends.

The dangerous half of turning it on is the bypass list. A maintenance
flag that 503s the page you turn it off from is worse than none, and the
list shipped with a hardcoded `/auth/` inherited from the template —
not a route in this project, whose login page is at `LOGIN_URL`. Those
cases are the bulk of what's tested here.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.contrib.auth.models import User

from apps.core import maintenance
from apps.core.security.maintenance import _is_bypass_path


@pytest.fixture(autouse=True)
def configured_install(monkeypatch):
    """Get `SetupRequiredMiddleware` out of the way.

    It sits one layer inside the maintenance middleware and 302s every
    human-facing route to `/setup/` until the install is finished — which
    needs a real clone on disk. Patching its two predicates is the only
    part of setup these tests care about; `middleware.py` imports
    `setup_state` lazily inside `__call__`, so module attributes are the
    right seam.
    """
    from apps.brainconfig import setup_state

    monkeypatch.setattr(setup_state, "needs_first_admin", lambda: False)
    monkeypatch.setattr(setup_state, "is_complete", lambda: True)


@pytest.fixture
def maintenance_on(db):
    maintenance.set_enabled(True, message="Reindexing the brain.")
    yield
    maintenance.clear()


def test_middleware_is_installed() -> None:
    assert "apps.core.security.MaintenanceModeMiddleware" in settings.MIDDLEWARE, (
        "The middleware is not in MIDDLEWARE, so the flag enforces nothing."
    )


def test_it_runs_after_authentication_middleware() -> None:
    """`request.user.is_staff` is the bypass condition; reading it before
    AuthenticationMiddleware would make every request look anonymous and
    503 the operator too."""
    chain = settings.MIDDLEWARE
    assert chain.index("apps.core.security.MaintenanceModeMiddleware") > chain.index(
        "django.contrib.auth.middleware.AuthenticationMiddleware"
    )


# ---- the bypass list --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/ops/",
        "/ops/settings/",
        "/login/",
        "/login/?next=/ops/",
        "/logout/",
        "/setup/",
        "/healthz",
        "/readyz",
        "/static/css/tw.css",
        "/.well-known/security.txt",
    ],
)
def test_the_operator_can_always_get_back_in(path: str) -> None:
    assert _is_bypass_path(path), (
        f"{path} is blocked during maintenance. Every path here is one the "
        "operator needs in order to reach the switch and turn it off."
    )


@pytest.mark.parametrize(
    "path",
    ["/", "/api/v1/get-note", "/mcp/", "/mcp/k/mcpurl_abc/", "/docs/"],
)
def test_consumer_surfaces_are_not_bypassed(path: str) -> None:
    """The flip side — a bypass list that swallowed everything would make
    the flag inert in a new way."""
    assert not _is_bypass_path(path)


def test_login_bypass_follows_the_setting_not_a_hardcoded_path(settings) -> None:
    """The original list hardcoded `/auth/`, which this project does not
    route. Derive it, so moving LOGIN_URL can't silently re-lock the door."""
    settings.LOGIN_URL = "/sign-in/"
    assert _is_bypass_path("/sign-in/")
    assert not _is_bypass_path("/login/")


# ---- enforcement ------------------------------------------------------------


@pytest.mark.django_db
def test_off_by_default(client) -> None:
    assert maintenance.is_enabled() is False
    assert client.get("/").status_code == 200


def test_anonymous_visitor_gets_503(client, maintenance_on) -> None:
    response = client.get("/")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert response.headers["Cache-Control"] == "no-store"
    assert b"Reindexing the brain." in response.content


def test_staff_keep_browsing(client, maintenance_on) -> None:
    """The window is usually *for* something the operator needs to look
    at on the live site."""
    user = User.objects.create_user("op", password="x" * 20, is_staff=True)
    client.force_login(user)
    assert client.get("/").status_code == 200


def test_the_switch_page_stays_reachable(client, maintenance_on) -> None:
    user = User.objects.create_user("op2", password="x" * 20, is_staff=True, is_superuser=True)
    client.force_login(user)
    assert client.get("/ops/settings/").status_code == 200


def test_api_gets_json_not_an_html_page(client, maintenance_on) -> None:
    """Consumers parse `{"error": {"code", "message"}}` for every other
    failure on this surface."""
    response = client.post(
        "/api/v1/get-note", data="{}", content_type="application/json"
    )
    assert response.status_code == 503
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.json()["error"]["code"] == "maintenance"
    assert response.headers["Retry-After"] == "60"


def test_health_checks_still_answer(client, maintenance_on) -> None:
    """Returning 503 to the platform's health probe during a maintenance
    window would get the container killed mid-window."""
    assert client.get("/healthz").status_code == 200


# ---- the operator control ---------------------------------------------------


@pytest.mark.django_db
def test_settings_page_can_turn_it_on_and_off(client) -> None:
    user = User.objects.create_user("op3", password="x" * 20, is_staff=True, is_superuser=True)
    client.force_login(user)
    try:
        client.post(
            "/ops/settings/",
            {"action": "maintenance", "enabled": "on", "maintenance_message": "Back at 5."},
        )
        assert maintenance.is_enabled() is True
        assert maintenance.get_message() == "Back at 5."

        # A post with the checkbox absent is "off" — that is what the
        # rendered turn-it-off button submits.
        client.post("/ops/settings/", {"action": "maintenance"})
        assert maintenance.is_enabled() is False
    finally:
        maintenance.clear()


@pytest.mark.django_db
def test_blank_message_keeps_the_existing_banner(client) -> None:
    user = User.objects.create_user("op4", password="x" * 20, is_staff=True, is_superuser=True)
    client.force_login(user)
    try:
        client.post(
            "/ops/settings/",
            {"action": "maintenance", "enabled": "on", "maintenance_message": "Original."},
        )
        client.post("/ops/settings/", {"action": "maintenance", "enabled": "on"})
        assert maintenance.get_message() == "Original."
    finally:
        maintenance.clear()


@pytest.mark.django_db
def test_toggling_leaves_an_audit_trail(client) -> None:
    from apps.events.models import Event

    user = User.objects.create_user("op5", password="x" * 20, is_staff=True, is_superuser=True)
    client.force_login(user)
    try:
        Event.objects.all().delete()
        client.post("/ops/settings/", {"action": "maintenance", "enabled": "on"})
        row = Event.objects.get(type="settings_change")
        assert row.details["action"] == "settings.maintenance_mode.toggled"
        assert row.details["before"]["enabled"] is False
        assert row.details["after"]["enabled"] is True
        assert row.details["actor_id"] == user.pk
    finally:
        maintenance.clear()


# ---- the 503 page itself ----------------------------------------------------


def test_the_try_again_control_needs_no_javascript() -> None:
    """It was a `<button @click="window.location.reload()">` with no
    x-data ancestor, which the enforced CSP drops anyway."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[3] / "templates/errors/maintenance.html"
    ).read_text(encoding="utf-8")
    body = template.split("{% block error_actions %}", 1)[1]
    assert "@click" not in body
    assert "x-on:" not in body
    assert "window.location" not in body
    assert "Try again" in body


def test_the_503_page_renders_its_reload_link(client, maintenance_on) -> None:
    response = client.get("/some/deep/path/")
    assert response.status_code == 503
    # The anchor points back at the path that was refused, so retrying
    # lands where the visitor was trying to go.
    assert b'href="/some/deep/path/"' in response.content
