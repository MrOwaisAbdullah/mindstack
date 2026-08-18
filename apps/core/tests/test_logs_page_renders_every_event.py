"""`/ops/logs/` must render every event type, not just the ones with `via`.

The connector-attribution work put the fallback in the template:

    {{ row.e.consumer|default:row.e.details.via|default:"—" }}

Django resolves a filter **argument** strictly. A missing key in
`{{ variable }}` renders as `string_if_invalid`; the same lookup as an
argument raises `VariableDoesNotExist` and takes the response with it.

Most events carry no `via` at all — `settings_change`, `csp_violation`,
`endpoint_error`, anything not a credentialed read. So a single one of
those anywhere in the selected window turned the whole logs page into a
500, and `settings_change` is emitted by saving any setting. Found by
loading the page on the running stack, where a `csp_violation` row from
an earlier session was enough.

`activity.json` does the same resolution and never had the bug, because
it does the lookup in Python. The page now does too.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.api_keys.models import APIKey
from apps.events.models import Event

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(client, monkeypatch):
    from apps.brainconfig import setup_state

    # Otherwise SetupRequiredMiddleware bounces every /ops/ request into
    # the wizard — setup is never "complete" in a fresh test database.
    monkeypatch.setattr(setup_state, "is_complete", lambda: True)
    user = User.objects.create_user(
        "logs-op", password="x" * 20, is_staff=True, is_superuser=True
    )
    client.force_login(user)
    return user


def _event(etype: str, details: dict, consumer=None) -> Event:
    return Event.objects.create(type=etype, details=details, consumer=consumer)


def test_an_event_with_no_via_does_not_500_the_page(client, operator) -> None:
    """The finding. Nothing about this row is unusual — it is what every
    settings save writes."""
    _event("settings_change", {"key": "DAILY_COST_CAP", "cleared": False})

    response = client.get("/ops/logs/")

    assert response.status_code == 200


def test_a_details_less_event_does_not_500_the_page(client, operator) -> None:
    _event("endpoint_error", {})

    assert client.get("/ops/logs/").status_code == 200


def _credential(client) -> str:
    """The attribution cell's value. Asserted on the context rather than
    the body: the raw `details` dict is also rendered on the row, so a
    `via` label appears in the HTML whether or not it won."""
    return client.get("/ops/logs/").context["event_rows"][0]["credential"]


def test_a_connector_read_still_shows_its_via_label(client, operator) -> None:
    _event("read", {"endpoint": "get-note", "via": "claude-connector"})

    assert _credential(client) == "claude-connector"


def test_an_api_key_read_still_shows_the_key(client, operator) -> None:
    """The FK wins over the label — it is the more specific attribution."""
    key = APIKey.objects.create(
        user=operator, name="a-consumer",
        prefix="mcpsk_abcd1234", key_hash="h" * 64, last_4="wxyz",
    )
    _event("read", {"endpoint": "get-note", "via": "should-not-win"}, consumer=key)

    assert _credential(client) == str(key)


def test_an_unattributed_event_falls_through_to_the_dash(client, operator) -> None:
    """"" in the context, and the template's own `|default:"—"` renders
    it — keeping the dash where a designer would look for it."""
    _event("settings_change", {"key": "MODEL"})

    assert _credential(client) == ""
    assert "—" in client.get("/ops/logs/").content.decode()
