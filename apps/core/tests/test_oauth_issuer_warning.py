"""The localhost-issuer warning must only fire when it is not simply true.

A fresh local stack logged this once per gunicorn worker — three times
before the operator had seen the wizard:

    OAUTH_ISSUER is 'http://localhost:8000' in production. ...
    check that ALLOWED_HOSTS is your real domain.

On `ALLOWED_HOSTS=localhost,127.0.0.1` that is not a misconfiguration,
it is an accurate description of a deliberate local run — the exact run
the getting-started docs walk someone through. The first thing a
newcomer read in their log was a warning about a setting they had set
correctly.

It also could not fire for any other reason. `_derive_public_origin`
runs first and overwrites a localhost issuer whenever ALLOWED_HOSTS
names a real host, so reaching the check with a localhost issuer means
ALLOWED_HOSTS named none — the condition the message tells you to go
check is the one guaranteed to hold whenever you read it.

Still worth flagging: a deployment that looks public but handed us no
hostname to derive from (`ALLOWED_HOSTS=*`). There the advice is
actionable, so that case stays loud.
"""
from __future__ import annotations

import logging

import pytest


def _settings_with(monkeypatch, **env):
    """Build a fresh Settings under a controlled environment."""
    from config.settings.env import Settings

    base = {
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "SECRET_KEY": "x" * 50,
        "DEBUG": "false",
    }
    base.update(env)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    return Settings()  # type: ignore[call-arg]


def _warnings_from(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "OAUTH_ISSUER" in r.getMessage()
    ]


def test_silent_when_allowed_hosts_is_loopback_only(monkeypatch, caplog) -> None:
    """The local-trial case. Nothing is wrong, so nothing is said."""
    with caplog.at_level(logging.WARNING):
        settings = _settings_with(monkeypatch, ALLOWED_HOSTS="localhost,127.0.0.1")
        settings.assert_prod_safe()

    assert _warnings_from(caplog) == []


def test_silent_when_a_real_domain_is_configured(monkeypatch, caplog) -> None:
    """The normal deployment. The issuer derives from the domain, so there
    is nothing to warn about — and this asserts the derivation, not just
    the silence."""
    with caplog.at_level(logging.WARNING):
        settings = _settings_with(monkeypatch, ALLOWED_HOSTS="brain.example.com")
        settings.assert_prod_safe()

    assert settings.OAUTH_ISSUER == "https://brain.example.com"
    assert _warnings_from(caplog) == []


def test_warns_when_the_deployment_looks_public_but_named_no_host(
    monkeypatch, caplog
) -> None:
    """`ALLOWED_HOSTS=*` is public-facing with nothing to derive from.
    This is the case the message was written for, and it must survive."""
    with caplog.at_level(logging.WARNING):
        settings = _settings_with(monkeypatch, ALLOWED_HOSTS="*")
        settings.assert_prod_safe()

    warnings = _warnings_from(caplog)
    assert len(warnings) == 1
    assert "ALLOWED_HOSTS" in warnings[0]


@pytest.mark.parametrize("hosts", ["localhost", "127.0.0.1", "::1", "localhost,::1"])
def test_every_loopback_spelling_is_quiet(monkeypatch, caplog, hosts: str) -> None:
    """`localhost`, `127.0.0.1` and `::1` are the same intent written three
    ways; a newcomer should not get a warning for picking a different one."""
    with caplog.at_level(logging.WARNING):
        settings = _settings_with(monkeypatch, ALLOWED_HOSTS=hosts)
        settings.assert_prod_safe()

    assert _warnings_from(caplog) == []
