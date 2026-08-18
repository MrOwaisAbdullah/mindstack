"""The tier guard skipped path arguments it did not recognise.

`tier_path_guard` is the PreToolUse hook that confines the reader agent's
Read/Grep/Glob to one tier snapshot. It is not defence in depth — the
module docstring is explicit that `cwd` does not constrain an absolute
path and an `allowed_tools` entry without a `(...)` specifier allows the
tool outright, so this hook is the only thing standing between a
public-tier agent and the private snapshot on the same volume.

It looped over the known path keys and did:

    if not raw or not isinstance(raw, str):
        continue

`continue` means allow. Every value that was not a plain string — a list
of paths, a dict, a number — was waved through unchecked.

Nothing exploits that today: Read/Grep/Glob each take a single string.
It matters because the SDK is a pinned dependency we bump deliberately,
and the failure mode of a bump that starts passing `path` as a list is
silent. A boundary that stops guarding when its input changes shape is
one that will be wrong exactly once, without saying so.

So an unrecognised shape is now denied, not skipped. A denial is visible
and recoverable; a silent allow is neither.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apps.reader.services.sdk_runner import tier_path_guard


def _decide(guard, tool_input: dict, tool_name: str = "Read") -> dict:
    return asyncio.run(
        guard({"tool_name": tool_name, "tool_input": tool_input}, "use-1", None)
    )


def _denied(result: dict) -> bool:
    out = (result or {}).get("hookSpecificOutput") or {}
    return out.get("permissionDecision") == "deny"


@pytest.fixture()
def guard(tmp_path):
    root = tmp_path / "views" / "public"
    root.mkdir(parents=True)
    (root / "note.md").write_text("in tier\n", encoding="utf-8")
    outside = tmp_path / "views" / "private"
    outside.mkdir(parents=True)
    (outside / "secret.md").write_text("out of tier\n", encoding="utf-8")
    return tier_path_guard(root), root, outside


class TestAnUnrecognisedShapeIsDenied:
    """Each of these was silently allowed before."""

    def test_a_list_of_paths(self, guard) -> None:
        g, _root, outside = guard

        assert _denied(_decide(g, {"path": [str(outside)]}, "Grep"))

    def test_a_dict(self, guard) -> None:
        g, _root, outside = guard

        assert _denied(_decide(g, {"file_path": {"p": str(outside)}}))

    def test_a_number(self, guard) -> None:
        g, _root, _outside = guard

        assert _denied(_decide(g, {"file_path": 12345}))

    def test_the_reason_names_the_key(self, guard) -> None:
        g, _root, outside = guard

        result = _decide(g, {"path": [str(outside)]}, "Grep")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        assert "path" in reason


class TestTheBehaviourThatAlreadyWorked:
    """Regression guards — these passed before and must keep passing."""

    def test_a_path_inside_the_tier_is_allowed(self, guard) -> None:
        g, root, _outside = guard

        assert not _denied(_decide(g, {"file_path": str(root / "note.md")}))

    def test_a_relative_path_inside_the_tier_is_allowed(self, guard) -> None:
        g, _root, _outside = guard

        assert not _denied(_decide(g, {"file_path": "note.md"}))

    def test_an_absolute_path_outside_the_tier_is_denied(self, guard) -> None:
        g, _root, outside = guard

        assert _denied(_decide(g, {"file_path": str(outside / "secret.md")}))

    def test_a_traversal_out_of_the_tier_is_denied(self, guard) -> None:
        g, _root, _outside = guard

        assert _denied(_decide(g, {"file_path": "../private/secret.md"}))

    def test_a_symlink_pointing_out_of_the_tier_is_denied(self, guard) -> None:
        """resolve() happens before the containment check."""
        g, root, outside = guard
        link = root / "link.md"
        try:
            link.symlink_to(outside / "secret.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this host")

        assert _denied(_decide(g, {"file_path": str(link)}))

    def test_a_missing_key_is_not_a_denial(self, guard) -> None:
        """A tool call carrying no path at all has nothing to contain."""
        g, _root, _outside = guard

        assert not _denied(_decide(g, {"pattern": "secret"}, "Grep"))

    def test_an_empty_string_is_not_a_denial(self, guard) -> None:
        """Absent and empty are the same thing: no path was named. The
        tool's own default applies, and `cwd` is the tier."""
        g, _root, _outside = guard

        assert not _denied(_decide(g, {"path": ""}, "Grep"))
