"""Clearing a required setting must not lock the operator out of the fix.

Setup completion is DERIVED from live predicates (setup_state's design),
so ticking "Clear the stored value" on ANTHROPIC_API_KEY — on
/ops/settings/ itself — flipped `is_complete()` to False and
`SetupRequiredMiddleware` then bounced every /ops/ request into the
wizard, including the settings page whose next render would have taken
the new key. Same shape maintenance mode had before its bypass list:
a state whose own switch it swallows.

The settings page is now exempt from the unfinished-setup redirect.
Everything else still routes to the wizard (that redirect is the
product working as designed for a genuinely half-configured install),
and the no-admin-at-all case still redirects unconditionally — an
unowned server has no operator to exempt.

No monkeypatching of `is_complete` here, deliberately: on the host test
environment there is no clone and no key, so setup is genuinely
incomplete and the middleware runs its real logic.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(client):
    user = User.objects.create_user(
        "locked-out-op", password="x" * 20, is_staff=True, is_superuser=True
    )
    client.force_login(user)
    return user


def test_settings_stays_reachable_while_setup_is_incomplete(client, operator) -> None:
    assert client.get("/ops/settings/").status_code == 200


def test_settings_survives_clearing_the_claude_key(client, operator) -> None:
    """The exact ejection route: clear a required setting FROM the
    settings page, then load the settings page again."""
    from apps.brainconfig import services

    services.set_value("ANTHROPIC_API_KEY", "sk-ant-something")
    client.post("/ops/settings/", {"clear__ANTHROPIC_API_KEY": "on", "action": "save"})
    assert not services.is_db_set("ANTHROPIC_API_KEY")
    assert client.get("/ops/settings/").status_code == 200


def test_every_other_ops_page_still_routes_to_the_wizard(client, operator) -> None:
    for path in ("/ops/", "/ops/tasks/", "/ops/health/"):
        response = client.get(path)
        assert response.status_code == 302, f"{path} did not redirect"
        assert response.url.startswith("/setup/"), f"{path} went to {response.url}"


def test_an_unowned_server_still_redirects_everything(client) -> None:
    """No admin exists → even the settings page goes to the wizard. An
    unowned install has no operator to exempt, and the account step is
    the only meaningful page."""
    response = client.get("/ops/settings/")
    assert response.status_code == 302
    assert response.url.startswith("/setup/")
