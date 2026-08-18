"""Tiers change together, or not at all — and a failure is not green.

Two findings, one root cause: the snapshot build was per-tier
build-and-swap, and the caller had already recorded `SyncRun.ok = True`
before any of it ran.

- `build_all` iterated tiers with no try/except, so a failure on tier 2
  left `public` at the new HEAD while `agents-only` and `private` sat at
  the old one, indefinitely. Tiers disagreeing is the one thing
  snapshots exist to prevent, and `get-raw` serves out of them.
- The swap was `rmtree(final)` then `rename(tmp, final)`. During the
  rmtree every read at that tier returned 422 "unknown path", and a
  process killed between the two calls left NO directory for the tier
  at all until the next successful sync — realistic, because the GitHub
  webhook runs `sync()` synchronously inside a gunicorn request under a
  60s timeout.
- `indexer.rebuild()` records `ok = True` once the DB index is
  consistent, which is before a single tier has been written. So the
  dashboard tile and `last_indexed_sha()` both reported a published
  brain that was not published.
"""
from __future__ import annotations

import json
import shutil

import pytest

from apps.brain.models import Entity, SyncRun
from apps.brain.services import indexer, snapshots, sync

pytestmark = pytest.mark.django_db


@pytest.fixture()
def views(tmp_path, settings, monkeypatch):
    """A brain with one note per tier, and a views dir to publish into."""
    repo = tmp_path / "repo"
    (repo / "knowledge" / "takes").mkdir(parents=True)
    for name, vis in (("pub", "public"), ("agt", "agents-only"), ("prv", "private")):
        (repo / "knowledge" / "takes" / f"{name}.md").write_text(
            f"---\nid: {name}\nvisibility: {vis}\n---\n\n# {name}\n", encoding="utf-8"
        )
        Entity.objects.create(
            entity_id=name, kind="take", path=f"knowledge/takes/{name}.md",
            title=name, visibility=vis,
        )
    settings.BRAIN_REPO_DIR = repo
    settings.BRAIN_VIEWS_DIR = tmp_path / "views"
    monkeypatch.setattr(snapshots.gitrepo, "head_sha", lambda: "a" * 40)
    return tmp_path / "views"


def _heads(views) -> dict[str, str]:
    return {
        tier: json.loads((views / tier / "_MANIFEST.json").read_text(encoding="utf-8"))["head"]
        for tier in snapshots.TIERS
    }


def _fail_on(monkeypatch, tier: str):
    real = snapshots.stage_tier

    def staged(t):
        if t == tier:
            raise OSError("disk full")
        return real(t)

    monkeypatch.setattr(snapshots, "stage_tier", staged)


class TestTiersMoveTogether:
    def test_a_failure_leaves_every_tier_at_the_old_state(self, views, monkeypatch):
        snapshots.build_all()
        before = _heads(views)

        monkeypatch.setattr(snapshots.gitrepo, "head_sha", lambda: "b" * 40)
        _fail_on(monkeypatch, "agents-only")
        with pytest.raises(OSError):
            snapshots.build_all()

        assert _heads(views) == before
        assert set(before.values()) == {"a" * 40}

    def test_no_tier_is_left_missing(self, views, monkeypatch):
        snapshots.build_all()
        _fail_on(monkeypatch, "private")
        with pytest.raises(OSError):
            snapshots.build_all()

        for tier in snapshots.TIERS:
            assert (views / tier).is_dir()
            assert (views / tier / "_MANIFEST.json").is_file()

    def test_a_failure_on_the_very_first_build_publishes_nothing(self, views, monkeypatch):
        _fail_on(monkeypatch, "public")
        with pytest.raises(OSError):
            snapshots.build_all()

        assert not (views / "public").exists()
        assert not (views / "agents-only").exists()

    def test_a_clean_build_moves_them_all(self, views, monkeypatch):
        snapshots.build_all()
        monkeypatch.setattr(snapshots.gitrepo, "head_sha", lambda: "c" * 40)
        snapshots.build_all()

        assert set(_heads(views).values()) == {"c" * 40}


class TestTheSwapSurvivesInterruption:
    def test_an_interrupted_swap_is_recovered_on_the_next_build(self, views):
        snapshots.build_all()
        # Exactly the state a kill between the two renames leaves.
        (views / "public").rename(views / ".old-public")
        assert not (views / "public").exists()

        snapshots.build_all()

        assert (views / "public" / "_MANIFEST.json").is_file()

    def test_recovery_restores_the_previous_snapshot_directly(self, views):
        snapshots.build_all()
        marker = views / "public" / "knowledge" / "takes" / "pub.md"
        assert marker.is_file()
        (views / "public").rename(views / ".old-public")

        snapshots.recover_interrupted_swaps()

        assert marker.is_file()
        assert not (views / ".old-public").exists()

    def test_recovery_does_not_touch_a_healthy_tier(self, views):
        snapshots.build_all()
        (views / ".old-public").mkdir()
        (views / ".old-public" / "STALE").write_text("x", encoding="utf-8")

        snapshots.recover_interrupted_swaps()

        assert not (views / "public" / "STALE").exists()

    def test_a_leftover_old_dir_does_not_break_the_next_swap(self, views):
        snapshots.build_all()
        (views / ".old-public").mkdir(exist_ok=True)
        snapshots.build_all()

        assert (views / "public" / "_MANIFEST.json").is_file()

    def test_the_live_tier_is_never_deleted_before_its_replacement_exists(
        self, views, monkeypatch
    ):
        """The old directory has to survive until the new one is in place;
        `rmtree` first is what made a crash unrecoverable."""
        snapshots.build_all()
        seen = []
        real_rmtree = shutil.rmtree

        def watched(path, *a, **kw):
            seen.append(str(path))
            return real_rmtree(path, *a, **kw)

        monkeypatch.setattr(snapshots.shutil, "rmtree", watched)
        snapshots.build_all()

        assert not any(p.endswith(("views\\public", "views/public")) for p in seen), seen


class TestTheRunIsNotGreenWhenTiersAreNot:
    def test_a_snapshot_failure_marks_the_run_not_ok(self, views, monkeypatch):
        run = indexer.rebuild(trigger="test")
        assert run.ok is True

        _fail_on(monkeypatch, "public")
        with pytest.raises(OSError):
            sync.publish_snapshots(run)

        run.refresh_from_db()
        assert run.ok is False
        assert "snapshot build failed" in run.error

    def test_last_indexed_sha_does_not_advertise_it(self, views, monkeypatch):
        run = indexer.rebuild(trigger="test")
        _fail_on(monkeypatch, "public")
        with pytest.raises(OSError):
            sync.publish_snapshots(run)

        assert sync.last_indexed_sha() == ""

    def test_a_degraded_event_is_emitted(self, views, monkeypatch):
        from apps.events.models import Event

        run = indexer.rebuild(trigger="test")
        _fail_on(monkeypatch, "public")
        with pytest.raises(OSError):
            sync.publish_snapshots(run)

        ev = Event.objects.filter(type="degraded").first()
        assert ev is not None
        assert ev.details["surface"] == "snapshots"

    def test_a_successful_publish_leaves_the_run_green(self, views):
        run = indexer.rebuild(trigger="test")
        sync.publish_snapshots(run)

        run.refresh_from_db()
        assert run.ok is True
        assert sync.last_indexed_sha() == run.commit_sha


class TestEveryReindexPathGoesThroughIt:
    """An index consistent on its own is not the brain being published.

    A caller that reindexes and then builds snapshots directly has a
    `SyncRun` row it will leave green if the build fails — that IS the
    finding, and it shipped in five separate places. A caller that only
    builds snapshots (`manage.py build_snapshots`) has no run to lie
    about, so it is not the shape being banned.
    """

    def test_no_caller_pairs_rebuild_with_a_bare_build_all(self):
        import re
        from pathlib import Path

        from django.conf import settings as dj

        offenders = []
        for path in Path(dj.BASE_DIR).glob("apps/**/*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            # The module that DEFINES the wrapper is the one place allowed
            # to call `build_all` beside a rebuild.
            if re.search(r"^def publish_snapshots\(", text, re.MULTILINE):
                continue
            if re.search(r"\bindexer\.rebuild\(", text) and re.search(
                r"\bsnapshots\.build_all\(", text
            ):
                offenders.append(str(path.relative_to(dj.BASE_DIR)))
        assert offenders == [], offenders

    def test_publish_snapshots_is_what_they_call(self):
        import re
        from pathlib import Path

        from django.conf import settings as dj

        for rel in (
            "apps/brain/services/sync.py",
            "apps/brainconfig/maintenance.py",
            "apps/brainconfig/setup_services.py",
            "apps/feeds/services/approval.py",
            "apps/brain/management/commands/brain_bootstrap.py",
        ):
            text = (Path(dj.BASE_DIR) / rel).read_text(encoding="utf-8")
            assert re.search(r"publish_snapshots\(", text), rel
