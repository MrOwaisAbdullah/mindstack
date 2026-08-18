"""Proposal → diff rendering (M2.4, grill C12d).

The approval UI renders proposals as DIFFS against the current clone,
never as executed markdown: injected-looking content must be visible,
not pretty. Pure text assembly — nothing here writes anywhere.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field


@dataclass
class FileDiff:
    path: str
    action: str  # create | update
    # (kind, text) per line; kind ∈ add | del | ctx | hunk
    lines: list[tuple[str, str]] = field(default_factory=list)
    added: int = 0
    removed: int = 0


def _classify(line: str) -> tuple[str, str]:
    if line.startswith("+"):
        return ("add", line[1:])
    if line.startswith("-"):
        return ("del", line[1:])
    if line.startswith("@@"):
        return ("hunk", line)
    return ("ctx", line[1:] if line.startswith(" ") else line)


def file_diff(path: str, action: str, new_content: str, old_content: str) -> FileDiff:
    d = FileDiff(path=path, action=action)
    raw = difflib.unified_diff(
        old_content.splitlines(),
        new_content.splitlines(),
        lineterm="",
        n=3,
    )
    for line in list(raw)[2:]:  # drop the ---/+++ header pair
        kind, text = _classify(line)
        d.lines.append((kind, text))
        if kind == "add":
            d.added += 1
        elif kind == "del":
            d.removed += 1
    return d


def build(proposal: dict, repo_dir) -> list[FileDiff]:
    """One FileDiff per proposed file, against the clone's current state."""
    out: list[FileDiff] = []
    for f in proposal.get("files") or []:
        path = str(f.get("path", ""))
        action = str(f.get("action", ""))
        target = repo_dir / path
        # Guard against path escapes before touching the filesystem (the
        # validator rejects these too — belt and braces for the UI).
        old = ""
        try:
            if target.resolve().is_relative_to(repo_dir.resolve()) and target.is_file():
                old = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            old = ""
        out.append(file_diff(path, action, str(f.get("content", "")), old))
    return out
