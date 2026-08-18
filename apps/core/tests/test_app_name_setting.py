"""`APP_NAME` is operator-editable on /ops/settings/ (SETUP-DESIGN.md §"UI").

Two things make this setting different from every other row in the
registry, and both are what these tests pin down:

  * it is read on **every** HTML render, including the 500 and
    maintenance pages — which exist precisely when the database is the
    broken thing. `app_name()` must therefore degrade to the boot-time
    env value instead of raising and taking the branded error page down
    with it;
  * it is free text that lands in `<title>` and the sidebar, so the save
    path bounds its length rather than storing whatever was pasted.

Deliberately DB-free: the host venv has no `django_redis`, so anything
marked `django_db` cannot stand up a cache backend here. The store is
stubbed at the `services.get` seam instead — which also means these run
without a migrated database.
"""
from __future__ import annotations

import pytest
from django.test import RequestFactory, override_settings

from apps.brainconfig import services, views


# ---- registry -----------------------------------------------------------


def _spec():
    return next(s for s in services.REGISTRY if s.key == "APP_NAME")


def test_app_name_is_editable_in_the_ui():
    spec = _spec()
    assert spec.max_len == 60
    assert not spec.secret, "the app name is displayed on every page — nothing to hide"


def test_app_name_does_not_let_env_win():
    """Unlike BRAIN_REPO_URL, a rename typed in the browser has to stick.

    `.env.example` ships `APP_NAME=BrainOutside`, so an `env_wins` spec
    here would make the Settings field appear to work and then quietly
    do nothing on every real deployment."""
    assert not _spec().env_wins


def test_default_is_the_boot_time_value():
    """So the Settings page shows the name actually in force when the
    store is empty, rather than a blank 'unset'."""
    from django.conf import settings as dj_settings

    assert _spec().default == dj_settings.APP_NAME


# ---- the accessor -------------------------------------------------------


def test_stored_value_wins(monkeypatch):
    monkeypatch.setattr(services, "get", lambda key: "Second Brain")
    assert services.app_name() == "Second Brain"


@override_settings(APP_NAME="EnvName")
def test_falls_back_to_env_when_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(services, "get", lambda key: "")
    assert services.app_name() == "EnvName"


@override_settings(APP_NAME="EnvName")
def test_database_failure_does_not_propagate(monkeypatch):
    """The regression this guards: a DB outage 500s, the 500 handler
    renders `errors/500.html`, its context processor reads APP_NAME, the
    read raises, and the operator gets Django's unbranded fallback page
    at the exact moment they need the branded one."""

    def boom(key):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(services, "get", boom)
    assert services.app_name() == "EnvName"


def test_context_processor_serves_the_effective_value(monkeypatch):
    from config import context_processors

    monkeypatch.setattr(services, "get", lambda key: "Second Brain")
    ctx = context_processors.app_meta(RequestFactory().get("/"))
    assert ctx["APP_NAME"] == "Second Brain"


# ---- the save path ------------------------------------------------------


class _Messages:
    """Stands in for `django.contrib.messages`; a RequestFactory request
    has no message storage attached."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def success(self, _request, text):
        self.sent.append(("success", text))

    def info(self, _request, text):
        self.sent.append(("info", text))

    def error(self, _request, text):
        self.sent.append(("error", text))


def _post(monkeypatch, value):
    """Drive `_save` with one APP_NAME field, recording what it stored."""
    stored: dict[str, str] = {}
    monkeypatch.setattr(services, "get", lambda key: "")
    monkeypatch.setattr(services, "is_db_set", lambda key: False)
    monkeypatch.setattr(
        services, "set_value", lambda key, val, actor=None: stored.update({key: val})
    )
    msgs = _Messages()
    monkeypatch.setattr(views, "messages", msgs)

    request = RequestFactory().post("/ops/settings/", {"value__APP_NAME": value})
    request.user = None
    views._save(request)
    return stored, msgs.sent


def test_a_reasonable_name_is_stored(monkeypatch):
    stored, sent = _post(monkeypatch, "Second Brain")
    assert stored == {"APP_NAME": "Second Brain"}
    assert sent == [("success", "Saved: APP_NAME")]


def test_an_overlong_name_is_rejected_not_truncated(monkeypatch):
    """Truncating would look like the save succeeded — the operator would
    only find out by reading their own browser tab."""
    stored, sent = _post(monkeypatch, "N" * 61)
    assert stored == {}
    assert [level for level, _ in sent] == ["error"]
    assert "APP_NAME" in sent[0][1]


@pytest.mark.parametrize("value", ["N" * 60, "  Padded  "])
def test_boundary_and_whitespace(monkeypatch, value):
    stored, _ = _post(monkeypatch, value)
    assert stored["APP_NAME"] == value.strip()
