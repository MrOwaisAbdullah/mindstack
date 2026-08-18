"""The REST path fires `EndpointCalled` — so `last_used_*` is true.

`APIKey.last_used_at/ip/request_id` are stamped by a subscriber on
`EndpointCalled`. The MCP proxy has always fired it (`_safe_record`);
the REST view only *stashed* `request._principal` for a
"RequestLogMiddleware" that exists in no settings file, so a key used
exclusively over REST never stamped its own columns. The ops page
worked around it by also consulting the event log, which kept the
display honest and the columns lying.

The fire happens at `view()`'s single exit, after `_dispatch`, so every
outcome (200, 401, 422, 429) is attributed — matching what MCP records.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.api_keys.models import APIKey
from apps.core.events import EndpointCalled, freeze
from apps.mind import consumers

pytestmark = pytest.mark.django_db


@pytest.fixture
def minted(db):
    user = User.objects.create_user("rest-called", password="x" * 20, is_staff=True)
    generated = consumers.create(
        user, name="rest-called-key", max_visibility="public", rate_limit_per_min=60
    )
    return generated


def _post(client, secret: str | None = None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {secret}"} if secret else {}
    return client.post(
        "/api/v1/ping", data="{}", content_type="application/json", **headers
    )


def test_an_authenticated_rest_call_fires_the_event(client, minted) -> None:
    with freeze() as fired:
        response = _post(client, minted.secret)
    assert response.status_code == 200
    called = [e for e in fired if isinstance(e, EndpointCalled)]
    assert len(called) == 1
    event = called[0]
    assert event.source == "rest"
    assert event.endpoint_slug == "ping"
    assert event.status_code == 200
    assert event.credential_id == minted.api_key.pk
    assert event.credential_kind == "api_key"
    assert event.latency_ms >= 1


def test_a_rest_only_key_stamps_its_own_last_used_columns(client, minted) -> None:
    """The finding's operator fact, on the columns themselves."""
    assert minted.api_key.last_used_at is None
    response = _post(client, minted.secret)
    assert response.status_code == 200
    key = APIKey.objects.get(pk=minted.api_key.pk)
    assert key.last_used_at is not None
    assert key.last_used_request_id != ""


def test_unauthenticated_calls_are_recorded_too(client) -> None:
    """MCP records failures with their status; REST now matches — a 401
    probe is traffic the operator should be able to see."""
    with freeze() as fired:
        response = _post(client, secret=None)
    assert response.status_code == 401
    called = [e for e in fired if isinstance(e, EndpointCalled)]
    assert len(called) == 1
    assert called[0].status_code == 401
    assert called[0].credential_id is None
