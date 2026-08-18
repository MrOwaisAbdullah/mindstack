"""Connector reads are attributed, not billed to "ops".

`Event.consumer` is an FK to `api_keys.APIKey`, so a URL-token
(connector) read can only land with consumer NULL — and the activity
feed's fallback rendered every NULL-consumer event as "ops": the
operator's own name on a stranger's traffic. The one fact an operator
checks before revoking a connector is whether it is being used; here it
was worse than unattributed, it was misattributed.

Schema-free fix: reads (and auth_denied) emitted for a non-APIKey
credential carry a `via` detail — "connector:<name>" for URL tokens —
and the three display surfaces (activity feed, dashboard recent list,
logs table) fall back to it before saying "ops"/"—".
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User

from apps.core.testing import call_endpoint
from apps.events.models import Event, credential_via
from apps.mind.endpoints import ListNotes
from apps.url_mcp_tokens.models import URLMCPToken


@pytest.fixture(autouse=True)
def configured_install(monkeypatch):
    from apps.brainconfig import setup_state

    monkeypatch.setattr(setup_state, "needs_first_admin", lambda: False)
    monkeypatch.setattr(setup_state, "is_complete", lambda: True)


# ---- the label itself (DB-free) -------------------------------------------


def test_url_token_gets_a_connector_label() -> None:
    token = URLMCPToken(name="claude-web", prefix="mcpurl_ab12")
    assert credential_via(token) == {"via": "connector:claude-web"}


def test_nameless_token_falls_back_to_its_prefix() -> None:
    token = URLMCPToken(name="", prefix="mcpurl_ab12")
    assert credential_via(token) == {"via": "connector:mcpurl_ab12"}


def test_no_credential_and_api_keys_stay_clean(db) -> None:
    from apps.api_keys.models import APIKey

    assert credential_via(None) == {}
    user = User.objects.create_user("via-test", password="x" * 20)
    key = APIKey(user=user, name="k", prefix="mcpsk_x", key_hash="h" * 64, last_4="abcd")
    assert credential_via(key) == {}


def test_a_future_credential_type_is_named_not_blank() -> None:
    class OAuthAccessToken:
        pass

    assert credential_via(OAuthAccessToken()) == {"via": "OAuthAccessToken"}


# ---- through a real endpoint ----------------------------------------------


@pytest.mark.django_db
def test_a_connector_read_event_carries_the_label() -> None:
    user = User.objects.create_user("via-endpoint", password="x" * 20)
    token = URLMCPToken.objects.create(
        user=user, name="claude-web", prefix="mcpurl_ab12",
        key_hash="h" * 64, last_4="cdef", max_visibility="agents-only",
    )
    async_to_sync(call_endpoint)(ListNotes, {}, credential=token)
    event = Event.objects.get(type="read")
    assert event.consumer_id is None  # the FK cannot hold a URL token
    assert event.details["via"] == "connector:claude-web"
    assert event.details["tier"] == "agents-only"


# ---- the display fallback --------------------------------------------------


@pytest.mark.django_db
def test_activity_feed_says_connector_not_ops(client) -> None:
    staff = User.objects.create_user(
        "via-activity", password="x" * 20, is_staff=True, is_superuser=True
    )
    client.force_login(staff)

    anchor = Event.objects.create(type="read", details={})  # sets the cursor
    Event.objects.create(
        type="read",
        details={"via": "connector:claude-web", "endpoint": "get-note", "tier": "public"},
    )
    genuinely_ops = Event.objects.create(type="read", details={"endpoint": "get-note"})

    payload = client.get(f"/ops/activity.json?after={anchor.pk}").json()
    consumers = [e["consumer"] for e in payload["events"]]
    assert "connector:claude-web" in consumers
    # The fallback must survive for events that really are the operator.
    assert consumers.count("ops") == 1
    assert genuinely_ops.pk in [e["id"] for e in payload["events"]]
