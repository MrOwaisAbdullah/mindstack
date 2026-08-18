"""Per-tier materialized snapshots — the visibility boundary (PLAN.md §5).

On every reindex the builder exports `/data/brain-views/<tier>/` for
public, agents-only, and private. Both serving layers read ONLY from
snapshots: the dumb endpoints serve snapshot bytes, and (M3) each SDK
agent gets `cwd` + Read scoped to its caller-tier snapshot — an agent
physically cannot read above tier, which is the structural fix for
grill C11 ("the tier simulator is a UI label, not a boundary").

Snapshot contents for tier T:
- every Entity whose visibility <= T (tier order: public < agents-only
  < private), copied at its repo-relative path;
- for the PUBLIC tier, inline `(agents-only: ...)` spans are STRIPPED
  from included files (CLAUDE.md §4 line-level rule, grill B6);
- `raw/` files reachable from an included entity (frontmatter `source`
  or a `raw/...md` mention in the body) — raw inherits the visibility
  of what links it and stays unreachable by browsing (grill B11);
  the private tier includes all of raw/;
- a GENERATED, tier-filtered `INDEX.md` built from the Entity table
  (the real INDEX.md is a repo-side view, never served — grill B7);
- `_MANIFEST.json` with head SHA, build time, and the file list.

NOT in any snapshot: CLAUDE.md, .claude/, eval/, PENDING.md, templates,
README — infrastructure, not content. Agent prompts are composed
server-side from the canonical clone, tier-appropriately (M3).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.brain.models import Entity
from apps.brain.services import gitrepo

log = logging.getLogger(__name__)

TIERS = ("public", "agents-only", "private")
TIER_ORDER = {"public": 0, "agents-only": 1, "private": 2}

#: Inline span convention from CLAUDE.md §4 — stripped below agents-only.
#: Scanned with a paren counter, NOT a regex. The regex this replaced was
#: `\(agents-only:[^)]*\)`, and `[^)]*` stops at the first closing paren:
#: a span holding a parenthetical aside, a citation or a URL like
#: `x.com/a_(b)` was matched only as far as that inner paren and the rest
#: of the secret went into the public snapshot verbatim. A pattern that
#: cannot count parens cannot enforce a containment boundary.
_SPAN_OPEN = "(agents-only:"
_RAW_REF_RE = re.compile(r"raw/[A-Za-z0-9_\-./]+?\.md")


def views_dir() -> Path:
    return Path(settings.BRAIN_VIEWS_DIR)


def _span_close(text: str, start: int) -> int | None:
    """Index of the paren that closes the span opened at `start`, or None.

    None means the span never closes — the input is malformed, and the
    caller must not guess where the secret ended.
    """
    depth = 0
    for k in range(start, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return None


def strip_agents_only_spans(text: str, source: str = "") -> str:
    """Remove `(agents-only: ...)` spans, counting nested parens.

    An unterminated span fails CLOSED: everything from the marker to the
    end of the file is dropped and the file is named in a warning. The
    alternative is guessing where the author meant the secret to end, and
    the old regex's guess — "it isn't a span at all" — published the lot.
    A note that reads short in public can be repaired; one that published
    a private aside cannot be unpublished.
    """
    out: list[str] = []
    i = 0
    while True:
        start = text.find(_SPAN_OPEN, i)
        if start < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:start])
        close = _span_close(text, start)
        if close is None:
            log.warning(
                "snapshot: unterminated '(agents-only:' span in %s — dropping "
                "everything from it to end of file rather than risk publishing "
                "it. Close the parenthesis in the brain repo.",
                source or "<unknown file>",
            )
            return "".join(out)
        i = close + 1


def _visible(tier: str):
    max_rank = TIER_ORDER[tier]
    return [e for e in Entity.objects.all() if TIER_ORDER.get(e.visibility, 2) <= max_rank]


def _inside_raw(repo: Path, rel: str) -> bool:
    """True when `rel` names a real file that is genuinely under `repo/raw/`.

    A prefix test is not enough. `_RAW_REF_RE` accepts `.` and `/`, so a
    reference like `raw/../identity/core.md` matches it and `is_file()`
    succeeds — which used to copy that file into whatever tier was being
    built, including public. Resolving first is what makes the `raw/`
    restriction mean anything, and it closes symlinks out of the repo too.
    """
    raw_root = (repo / "raw").resolve()
    try:
        target = (repo / rel).resolve()
    except OSError:
        return False
    if raw_root not in target.parents:
        return False
    return target.is_file()


def _linked_raw_paths(repo: Path, entities: list[Entity]) -> set[str]:
    """raw/ files referenced by the given entities (source field or body)."""
    found: set[str] = set()
    for e in entities:
        candidates: set[str] = set()
        if e.source.startswith("raw/"):
            candidates.add(e.source)
        p = repo / e.path
        if p.exists():
            candidates.update(_RAW_REF_RE.findall(p.read_text(encoding="utf-8", errors="replace")))
        for c in candidates:
            if _inside_raw(repo, c):
                found.add(c)
            else:
                log.warning("ignoring raw ref %r in %s (outside raw/)", c, e.path)
    return found


def _generated_index(tier: str, entities: list[Entity], head: str) -> str:
    lines = [
        f"# INDEX — generated view for tier `{tier}` (HEAD {head[:12]})",
        "",
        "One line per entity visible at this tier. Format:",
        "`- [kind] title — description | status | last-verified | path`",
        "",
    ]
    by_section = {
        "Identity": [e for e in entities if e.kind == "identity"],
        "Projects": [e for e in entities if e.kind == "project"],
        "Knowledge": [e for e in entities if e.kind in {"take", "story", "lesson", "fact"}],
        "Catalogs & lenses": [e for e in entities if e.kind in {"catalog", "lens"}],
    }
    for section, ents in by_section.items():
        if not ents:
            continue
        lines.append(f"## {section}")
        for e in sorted(ents, key=lambda x: (x.kind, x.entity_id)):
            desc = e.description or e.title
            parts = [f"- [{e.kind}] {e.title} — {desc}"]
            if e.status and e.status != "current":
                parts.append(f"status: {e.status}")
            if e.last_verified:
                parts.append(f"last-verified: {e.last_verified.isoformat()}")
            parts.append(e.path)
            lines.append(" | ".join(parts))
        lines.append("")
    return "\n".join(lines)


def _tmp_dir(tier: str) -> Path:
    return views_dir() / f".tmp-{tier}"


def _old_dir(tier: str) -> Path:
    return views_dir() / f".old-{tier}"


def stage_tier(tier: str) -> dict[str, object]:
    """Build one tier's snapshot into its temp dir. Nothing is swapped."""
    repo = gitrepo.repo_dir()
    head = gitrepo.head_sha()
    entities = _visible(tier)
    strip = tier == "public"

    tmp = _tmp_dir(tier)
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    files: list[str] = []

    def emit(rel: str, content: str | bytes) -> None:
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(content, encoding="utf-8", newline="\n")
        files.append(rel)

    for e in entities:
        src = repo / e.path
        if not src.is_file():
            continue  # drift; the sync layer logs it
        text = src.read_text(encoding="utf-8", errors="replace")
        emit(e.path, strip_agents_only_spans(text, source=e.path) if strip else text)

    for rel in sorted(_linked_raw_paths(repo, entities) if tier != "private" else _all_raw(repo)):
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        emit(rel, strip_agents_only_spans(text, source=rel) if strip else text)

    emit("INDEX.md", _generated_index(tier, entities, head))
    manifest = {
        "tier": tier,
        "head": head,
        "built_at": timezone.now().isoformat(),
        "entity_count": len(entities),
        "files": sorted(files),
    }
    emit("_MANIFEST.json", json.dumps(manifest, indent=2))
    return manifest


def swap_in(tier: str) -> None:
    """Move a staged tier into place, keeping the old one until it is.

    The swap used to be `rmtree(final)` then `rename(tmp, final)`. During
    the rmtree — a whole tier of files — every read at that tier returned
    422 "unknown path", and a process killed between the two calls left
    NO directory for the tier at all, until whenever the next successful
    sync happened. That is realistic: the GitHub webhook runs `sync()`
    synchronously inside a gunicorn request under a 60s timeout.

    Renaming the live directory aside instead makes the visible window
    two renames wide instead of a full delete, and leaves the previous
    snapshot on disk so `recover_interrupted_swaps()` can put it back.
    """
    tmp, final, old = _tmp_dir(tier), tier_dir(tier), _old_dir(tier)
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    tmp.rename(final)
    if old.exists():
        # Slow, and nothing reads it any more — a failure here (the
        # documented Windows bind-mount PermissionError) costs disk, not
        # correctness, and the next build clears it.
        try:
            shutil.rmtree(old)
        except OSError:
            log.warning("snapshots: could not remove %s yet", old, exc_info=True)


def recover_interrupted_swaps() -> None:
    """Put back a tier whose swap was interrupted mid-rename."""
    for tier in TIERS:
        final, old = tier_dir(tier), _old_dir(tier)
        if not final.exists() and old.is_dir():
            log.warning("snapshots: restoring %s from an interrupted swap", tier)
            old.rename(final)


def _all_raw(repo: Path) -> set[str]:
    """Every markdown file under raw/, at any depth.

    Was `glob("*.md")` — top level only — while `_RAW_REF_RE` explicitly
    matches nested paths. A note linking `raw/interviews/2024-06.md` put
    that file in the agents-only snapshot (via `_linked_raw_paths`) but not
    in the private one, so private-tier `get-raw` 404'd where agents-only
    succeeded, contradicting this module's own "private includes all of
    raw/" contract.
    """
    if not (repo / "raw").is_dir():
        return set()
    return {
        p.relative_to(repo).as_posix()
        for p in (repo / "raw").rglob("*.md")
        if p.name != "README.md" and _inside_raw(repo, p.relative_to(repo).as_posix())
    }


def build_all() -> dict[str, dict[str, object]]:
    """Build every tier under the repo lock, then swap them in together.

    Build-and-swap per tier meant a failure on tier 2 left `public` at
    the new HEAD while `agents-only` and `private` sat at the old one,
    indefinitely — tiers disagreeing is the one thing snapshots exist to
    prevent, and `get-raw` serves out of them. Worse, the caller had
    already recorded `SyncRun.ok = True`, so the health tile stayed
    green over it.

    Staging every tier first makes the failure mode "nothing changed"
    rather than "some tiers changed". The swap phase is renames only.
    """
    with gitrepo.repo_lock():
        recover_interrupted_swaps()
        manifests = {tier: stage_tier(tier) for tier in TIERS}
        for tier in TIERS:
            swap_in(tier)
        return manifests


def tier_dir(tier: str) -> Path:
    if tier not in TIER_ORDER:
        raise ValueError(f"unknown tier: {tier}")
    return views_dir() / tier


def manifest(tier: str) -> dict[str, object] | None:
    p = tier_dir(tier) / "_MANIFEST.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
