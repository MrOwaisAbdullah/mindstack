"""The PreToolUse guard is what confines Grep/Glob to one tier snapshot.

Not cwd, and not `allowed_tools`. Both were assumed to do it and neither
does: cwd does not constrain an absolute `path=`, and the SDK treats an
`allowed_tools` entry with no `(...)` specifier as allowing the whole tool
outright. All three tier snapshots share one mounted volume, so an
unconfined Grep with `output_mode="content"` reads private note bodies out
of a public-tier run. These tests pin the guard so that cannot come back.

DB-free on purpose (see CLAUDE.md — the host venv has no django_redis).
"""
from __future__ import annotations

import asyncio

import pytest

from apps.reader.services.sdk_runner import tier_path_guard


def _run(guard, tool_name: str, tool_input: dict):
    return asyncio.run(
        guard({"tool_name": tool_name, "tool_input": tool_input}, "tu_1", {"signal": None})
    )


def _denied(result) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.fixture()
def tiers(tmp_path):
    """Two sibling tier snapshots, as they sit on the shared volume."""
    public = tmp_path / "public"
    private = tmp_path / "private"
    (public / "notes").mkdir(parents=True)
    private.mkdir()
    (public / "notes" / "ok.md").write_text("public note", encoding="utf-8")
    (private / "secret.md").write_text("private note", encoding="utf-8")
    return public, private


def test_allows_paths_inside_the_tier(tiers):
    public, _ = tiers
    guard = tier_path_guard(public)

    assert _run(guard, "Read", {"file_path": str(public / "notes" / "ok.md")}) == {}
    assert _run(guard, "Grep", {"path": str(public)}) == {}
    # Relative paths resolve against the tier root (the agent's cwd).
    assert _run(guard, "Glob", {"path": "notes"}) == {}


def test_denies_absolute_path_into_a_sibling_tier(tiers):
    """The finding: a public-tier run grepping the private snapshot."""
    public, private = tiers
    guard = tier_path_guard(public)

    assert _denied(_run(guard, "Grep", {"path": str(private)}))
    assert _denied(_run(guard, "Read", {"file_path": str(private / "secret.md")}))
    assert _denied(_run(guard, "Glob", {"path": str(private)}))


def test_denies_relative_traversal_out_of_the_tier(tiers):
    public, _ = tiers
    guard = tier_path_guard(public)

    assert _denied(_run(guard, "Grep", {"path": "../private"}))
    assert _denied(_run(guard, "Read", {"file_path": "../private/secret.md"}))
    assert _denied(_run(guard, "Read", {"file_path": "notes/../../private/secret.md"}))


def test_denies_symlink_escape(tiers):
    """Resolution happens before the check, so a symlink out is refused."""
    public, private = tiers
    try:
        (public / "escape").symlink_to(private, target_is_directory=True)
    except (OSError, NotImplementedError):  # Windows without developer mode
        pytest.skip("symlinks not permitted on this host")

    guard = tier_path_guard(public)
    assert _denied(_run(guard, "Grep", {"path": "escape"}))
    assert _denied(_run(guard, "Read", {"file_path": "escape/secret.md"}))


def test_tier_root_itself_is_allowed(tiers):
    public, _ = tiers
    guard = tier_path_guard(public)

    assert _run(guard, "Grep", {"path": str(public)}) == {}
    assert _run(guard, "Glob", {"path": "."}) == {}


def test_calls_without_a_path_are_untouched(tiers):
    """Grep with no `path` searches cwd, which is already the tier root."""
    public, _ = tiers
    guard = tier_path_guard(public)

    assert _run(guard, "Grep", {"pattern": "anything"}) == {}
    assert _run(guard, "Grep", {"pattern": "x", "path": ""}) == {}
    assert _run(guard, "Grep", {"pattern": "x", "path": None}) == {}
