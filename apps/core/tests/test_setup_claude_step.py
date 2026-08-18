"""The wizard's Claude step must not persist a credential that failed.

The step is marked done on key *presence* (`claude_configured`), and the
old order was save-then-test: `Test connection` stored the pasted key
first, ran the probe second. A failing test therefore still completed
setup with a dead credential — the wizard said "Connection failed" in
red and the step lit up green anyway. The adjacent write step verifies
before it stores; this pins the Claude step to the same rule.

The probe is faked at the sdk_runner seam — these tests must never
spawn a real CLI. What's under test is the ordering: which key the
probe receives, and what gets stored on each outcome.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.brainconfig import services as cfg
from apps.reader.services import sdk_runner

pytestmark = pytest.mark.django_db


class _FakeRun:
    def __init__(self, ok: bool):
        self.ok = ok
        self.model = "claude-test"
        self.duration_ms = 12
        self.usage = {"input_tokens": 1, "output_tokens": 1}
        self.error_class = "" if ok else "AuthenticationError"


@pytest.fixture
def operator(client):
    user = User.objects.create_user(
        "claude-step-op", password="x" * 20, is_staff=True, is_superuser=True
    )
    client.force_login(user)
    return user


@pytest.fixture
def probe(monkeypatch):
    """Capture what the view asks the probe to test, and script the verdict."""
    calls: dict = {"candidate_key": None, "ok": True}

    def fake_test_connection(**kwargs):
        calls["candidate_key"] = kwargs.get("candidate_key")
        return _FakeRun(calls["ok"])

    monkeypatch.setattr(sdk_runner, "test_connection", fake_test_connection)
    return calls


def test_a_failing_test_stores_nothing(client, operator, probe) -> None:
    probe["ok"] = False
    client.post("/setup/claude/", {"key": "sk-ant-api-dead", "action": "test"})
    assert cfg.get("ANTHROPIC_API_KEY") == "", (
        "a credential that failed its own test was persisted — the step "
        "completes on presence, so setup just finished with a dead key"
    )


def test_the_probe_receives_the_pasted_key_not_the_stored_one(client, operator, probe) -> None:
    cfg.set_value("ANTHROPIC_API_KEY", "sk-ant-api-old")
    client.post("/setup/claude/", {"key": "sk-ant-api-new", "action": "test"})
    assert probe["candidate_key"] == "sk-ant-api-new"


def test_a_passing_test_stores_the_key(client, operator, probe) -> None:
    probe["ok"] = True
    client.post("/setup/claude/", {"key": "sk-ant-api-good", "action": "test"})
    assert cfg.get("ANTHROPIC_API_KEY") == "sk-ant-api-good"


def test_blank_paste_probes_the_stored_key(client, operator, probe) -> None:
    """Settings-page behaviour, preserved: testing with nothing pasted
    checks whatever is already stored — and must not blank it."""
    cfg.set_value("ANTHROPIC_API_KEY", "sk-ant-api-stored")
    client.post("/setup/claude/", {"key": "", "action": "test"})
    assert probe["candidate_key"] == ""
    assert cfg.get("ANTHROPIC_API_KEY") == "sk-ant-api-stored"


def test_continue_still_saves_on_presence(client, operator, probe) -> None:
    """The operator may proceed untested — that is their explicit call,
    and the page says so. Presence completes the step as before."""
    response = client.post("/setup/claude/", {"key": "sk-ant-api-untested", "action": "continue"})
    assert cfg.get("ANTHROPIC_API_KEY") == "sk-ant-api-untested"
    assert response.status_code == 302
    assert response.url.endswith("/build/")
