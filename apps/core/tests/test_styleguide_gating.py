"""The style guide is a development-only review aid and must never be
servable on a deployment.

It is gated twice on purpose:
  * `brainconfig/urls.py` only appends the route when `settings.DEBUG`, so
    a production process has no such URL at all;
  * the view itself re-checks and raises Http404, because the urlconf test
    happens once at import and a long-lived process that had DEBUG flipped
    underneath it would otherwise keep serving a stale route.

The second gate is the one worth testing: it is the one that still holds
when the first is bypassed.

Deliberately DB-free. `staff_member_required` only reads attributes off
`request.user`, and the view touches no models, so a stub user keeps these
runnable on the host venv -- which has no `django_redis`, so anything
marked `django_db` cannot even set up its cache backend here.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from django.http import Http404
from django.test import RequestFactory, override_settings

from apps.brainconfig.styleguide_views import styleguide


@dataclass
class StubUser:
    """Just enough user for `staff_member_required` and the base template."""
    is_staff: bool
    is_active: bool = True
    is_authenticated: bool = True
    is_superuser: bool = False
    username: str = "stub"

    def get_username(self) -> str:
        return self.username


def _request(user):
    request = RequestFactory().get("/ops/styleguide/")
    request.user = user
    return request


def test_view_refuses_to_render_when_debug_is_off():
    with override_settings(DEBUG=False):
        with pytest.raises(Http404):
            styleguide(_request(StubUser(is_staff=True)))


def test_view_renders_for_staff_when_debug_is_on():
    with override_settings(DEBUG=True):
        response = styleguide(_request(StubUser(is_staff=True)))
    assert response.status_code == 200


def test_non_staff_never_reaches_the_view():
    """`staff_member_required` wraps the view, so an ordinary signed-in user
    is bounced before the DEBUG check runs at all."""
    with override_settings(DEBUG=True):
        response = styleguide(_request(StubUser(is_staff=False)))
    assert response.status_code in (302, 403)
