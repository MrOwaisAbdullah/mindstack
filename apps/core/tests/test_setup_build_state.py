"""A zero-entity build must not trap the operator in the wizard.

`brain_built()` required `Entity.objects.exists()`. A build that ran fine
over a repo that indexed to zero notes reported done, never satisfied the
step, and `SetupRequiredMiddleware` then bounced every /ops/ request back
to /setup/ — where `finish` had just cleared the progress record, so the
operator saw a fresh "Build my brain" button. Infinite loop, no
explanation. The official template ships three identity files, which is
exactly why this only ever surfaced on a stranger's repo.
"""
from __future__ import annotations

import pytest

from apps.brain.models import Entity, SyncRun
from apps.brainconfig import setup_state


@pytest.fixture()
def valid_repo(monkeypatch):
    monkeypatch.setattr(
        setup_state, "brain_built", setup_state.brain_built
    )  # keep the real predicate
    from apps.brain.services import gitrepo

    monkeypatch.setattr(gitrepo, "is_valid_repo", lambda: True)


@pytest.mark.django_db
def test_not_built_before_any_sync_has_run(valid_repo):
    assert setup_state.brain_built() is False


@pytest.mark.django_db
def test_built_after_a_successful_sync_with_zero_entities(valid_repo):
    """The finding: an empty-but-valid brain is a legitimate state."""
    SyncRun.objects.create(trigger="setup", ok=True)

    assert Entity.objects.count() == 0
    assert setup_state.brain_built() is True


@pytest.mark.django_db
def test_built_with_entities(valid_repo):
    SyncRun.objects.create(trigger="setup", ok=True)
    Entity.objects.create(path="a.md", entity_id="a", kind="take", title="t")

    assert setup_state.brain_built() is True


@pytest.mark.django_db
def test_a_failed_sync_alone_does_not_count_as_built(valid_repo):
    SyncRun.objects.create(trigger="setup", ok=False, error="boom")

    assert setup_state.brain_built() is False


@pytest.mark.django_db
def test_entities_alone_still_count(valid_repo):
    """Upgrades: an install indexed before SyncRun history was kept."""
    Entity.objects.create(path="a.md", entity_id="a", kind="take", title="t")

    assert setup_state.brain_built() is True


@pytest.mark.django_db
def test_never_built_without_a_valid_clone(monkeypatch):
    from apps.brain.services import gitrepo

    monkeypatch.setattr(gitrepo, "is_valid_repo", lambda: False)
    SyncRun.objects.create(trigger="setup", ok=True)
    Entity.objects.create(path="a.md", entity_id="a", kind="take", title="t")

    assert setup_state.brain_built() is False
