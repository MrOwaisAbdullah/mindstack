"""Trusted-proxy client IP resolution.

The property under test is not "does it read the header" — it is **when
does it refuse to**. A resolver that believes the header unconditionally
is strictly worse than one that always returns REMOTE_ADDR: it hands any
caller the ability to forge the address that the admin-login lockout,
the honeypot counter and ADMIN_IP_ALLOWLIST key on.

DB-free by construction (CLAUDE.md: keep new tests DB-free where
possible) — `RequestFactory` builds requests without a database.
"""
from __future__ import annotations

import pytest
from django.test import RequestFactory, override_settings

from apps.core.security import client_ip as mod

CF = "CF-Connecting-IP"
PROXY = "10.1.2.3"          # a Traefik container on the docker network
CALLER = "203.0.113.7"      # the real end user
ATTACKER = "198.51.100.9"   # someone reaching the origin directly

# The resolver caches parsed CIDRs across calls; each override_settings
# block below uses a distinct value so a stale cache would show up as a
# failure rather than silently passing.
proxied = override_settings(
    TRUSTED_PROXY_IP_HEADER=CF, TRUSTED_PROXY_IPS=["10.1.0.0/16"]
)


@pytest.fixture(autouse=True)
def _clear_network_cache():
    mod._cached_networks.cache_clear()
    yield
    mod._cached_networks.cache_clear()


def _req(remote_addr: str, **headers):
    meta = {"REMOTE_ADDR": remote_addr}
    for name, value in headers.items():
        meta["HTTP_" + name.upper().replace("-", "_")] = value
    return RequestFactory().get("/", **meta)


# ---- default posture: nothing configured, nothing trusted ----------------


@override_settings(TRUSTED_PROXY_IP_HEADER="", TRUSTED_PROXY_IPS=[])
def test_unconfigured_returns_remote_addr():
    assert mod.client_ip(_req(PROXY)) == PROXY


@override_settings(TRUSTED_PROXY_IP_HEADER="", TRUSTED_PROXY_IPS=[])
def test_unconfigured_ignores_the_header_entirely():
    """The pre-existing `health.py` bug: a caller sets the header and is
    believed. With no proxy configured the header must not be read."""
    req = _req(ATTACKER, **{CF: "127.0.0.1", "X-Forwarded-For": "127.0.0.1"})
    assert mod.client_ip(req) == ATTACKER


# ---- configured and trusted ---------------------------------------------


@proxied
def test_trusted_peer_header_is_used():
    assert mod.client_ip(_req(PROXY, **{CF: CALLER})) == CALLER


@proxied
def test_forwarded_for_style_chain_takes_the_leftmost_entry():
    req = _req(PROXY, **{CF: f"{CALLER}, 172.16.0.1, 10.1.2.3"})
    assert mod.client_ip(req) == CALLER


@proxied
def test_trusted_peer_without_the_header_falls_back_to_peer():
    assert mod.client_ip(_req(PROXY)) == PROXY


# ---- configured but the request did not come through the proxy ----------


@proxied
def test_untrusted_peer_cannot_spoof_the_header():
    """The whole point. Someone reaching the origin directly sets
    CF-Connecting-IP to whatever they like; we must ignore it."""
    req = _req(ATTACKER, **{CF: CALLER})
    assert mod.client_ip(req) == ATTACKER


@proxied
def test_untrusted_peer_cannot_forge_a_loopback_address():
    """A forged loopback is the highest-value spoof: `_is_local_request`
    downgrades the exposure warning, and an ADMIN_IP_ALLOWLIST written
    for a local install would admit the caller."""
    req = _req(ATTACKER, **{CF: "127.0.0.1"})
    assert mod.client_ip(req) == ATTACKER


# ---- malformed input degrades to the peer, never to None ----------------


@proxied
def test_garbage_header_value_falls_back_to_peer():
    assert mod.client_ip(_req(PROXY, **{CF: "not-an-ip"})) == PROXY


@proxied
def test_empty_header_value_falls_back_to_peer():
    assert mod.client_ip(_req(PROXY, **{CF: "   "})) == PROXY


@override_settings(TRUSTED_PROXY_IP_HEADER=CF, TRUSTED_PROXY_IPS=["not-a-cidr"])
def test_invalid_cidr_entry_is_dropped_and_trusts_nobody():
    """A typo must narrow trust, not widen it."""
    assert mod.client_ip(_req(PROXY, **{CF: CALLER})) == PROXY


@override_settings(TRUSTED_PROXY_IP_HEADER=CF, TRUSTED_PROXY_IPS=["10.1.0.0/16", "bad"])
def test_valid_entries_survive_an_invalid_neighbour():
    assert mod.client_ip(_req(PROXY, **{CF: CALLER})) == CALLER


# ---- ipv6 ---------------------------------------------------------------


@override_settings(TRUSTED_PROXY_IP_HEADER=CF, TRUSTED_PROXY_IPS=["2001:db8::/32"])
def test_ipv6_peer_and_caller():
    req = _req("2001:db8::1", **{CF: "2001:db8:beef::9"})
    assert mod.client_ip(req) == "2001:db8:beef::9"


# ---- peer_ip is always the raw socket address ---------------------------


@proxied
def test_peer_ip_ignores_the_header():
    req = _req(PROXY, **{CF: CALLER})
    assert mod.peer_ip(req) == PROXY
    assert mod.client_ip(req) == CALLER


# ---- the exposure check no longer believes a forged header --------------
#
# `health._is_local_request` downgrades the "your ops UI is exposed"
# danger to a benign "local install" note. It used to read
# `X-Forwarded-For` unconditionally, so a caller who sent
# `Host: localhost` + `X-Forwarded-For: 127.0.0.1` could talk the panel
# out of its own warning.
#
# NB: these use 93.184.216.34 rather than a 198.51.100.x / 203.0.113.x
# documentation address, because Python's `ip_address().is_private` is
# True for the TEST-NET ranges — `_looks_local` would call them local and
# the test would pass for the wrong reason.

PUBLIC = "93.184.216.34"


@override_settings(TRUSTED_PROXY_IP_HEADER="", TRUSTED_PROXY_IPS=[])
def test_exposure_check_ignores_forged_forwarded_for():
    from apps.brainconfig import health

    req = RequestFactory().get(
        "/ops/health/",
        REMOTE_ADDR=PUBLIC,
        HTTP_HOST="localhost",
        HTTP_X_FORWARDED_FOR="127.0.0.1",
    )
    assert health._client_ip(req) == PUBLIC
    assert health._is_local_request(req) is False


@override_settings(TRUSTED_PROXY_IP_HEADER="", TRUSTED_PROXY_IPS=[])
def test_exposure_check_still_recognises_a_real_local_dev_request():
    from apps.brainconfig import health

    req = RequestFactory().get("/ops/health/", REMOTE_ADDR="127.0.0.1", HTTP_HOST="localhost")
    assert health._is_local_request(req) is True


@proxied
def test_exposure_detail_names_the_peer_to_add_when_untrusted():
    """The misconfigured case must be actionable: an operator who set the
    header but not the peer list needs to be told which address to add."""
    from apps.brainconfig import health

    req = _req(ATTACKER, **{CF: CALLER})
    detail = health._address_detail(req)
    assert ATTACKER in detail
    assert "TRUSTED_PROXY_IPS" in detail


@override_settings(TRUSTED_PROXY_IP_HEADER="", TRUSTED_PROXY_IPS=[])
def test_exposure_detail_names_the_peer_when_unconfigured():
    from apps.brainconfig import health

    detail = health._address_detail(_req(PROXY))
    assert PROXY in detail
    assert "TRUSTED_PROXY_IP_HEADER" in detail


# ---- boot-time guard against half a configuration -----------------------


def _prod_settings(**overrides):
    from config.settings.env import Settings

    base = dict(
        SECRET_KEY="x" * 64,
        ALLOWED_HOSTS=["example.com"],
        DJANGO_ADMIN_URL_PATH="some-random-slug/",
        FIELD_ENCRYPTION_KEY="k" * 44,
        DEV_LOGIN_ENABLED=False,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize(
    "overrides",
    [
        {"TRUSTED_PROXY_IP_HEADER": CF, "TRUSTED_PROXY_IPS": []},
        {"TRUSTED_PROXY_IP_HEADER": "", "TRUSTED_PROXY_IPS": ["10.0.0.0/8"]},
    ],
    ids=["header-without-peers", "peers-without-header"],
)
def test_half_a_proxy_configuration_refuses_to_boot(overrides):
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured, match="TRUSTED_PROXY"):
        _prod_settings(**overrides).assert_prod_safe()


@pytest.mark.parametrize(
    "overrides",
    [
        {"TRUSTED_PROXY_IP_HEADER": "", "TRUSTED_PROXY_IPS": []},
        {"TRUSTED_PROXY_IP_HEADER": CF, "TRUSTED_PROXY_IPS": ["10.0.0.0/8"]},
    ],
    ids=["neither", "both"],
)
def test_both_or_neither_boots(overrides):
    _prod_settings(**overrides).assert_prod_safe()
