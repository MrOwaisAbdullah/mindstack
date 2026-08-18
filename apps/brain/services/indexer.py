"""Frontmatter → Entity indexer.

The repo has five frontmatter dialects (grill B8) — one parser per kind:

- knowledge notes  (`knowledge/<type>/*.md`): full note schema from
  CLAUDE.md §3 (`id`, `type`, `topics`, `projects`, `source`,
  `source_url`, `date`, `status`, `superseded_by`, `visibility`).
- project cards    (`projects/*.md`): `id`, `kind: project`, `topics`,
  `visibility`, `last-verified`, free-text status inside the body's
  Status line (kept free-text).
- identity files   (`identity/*.md`): `id`, `visibility`, `last-verified`.
- lenses           (`lenses/*.md`): `lens: <name>` + promoted fields.
- catalogs         (`content-catalog/*.md` minus README): no frontmatter;
  title from the first H1.

Visibility resolution follows CLAUDE.md §4's path-default table:
explicit frontmatter wins; identity/knowledge/projects missing →
agents-only; catalogs + lenses → public. Non-entity paths (raw/, eval/,
templates, INDEX.md, PENDING.md, CLAUDE.md, .claude/, README) are never
indexed; raw/ visibility is resolved at serve time via linking notes.

INDEX.md is read ONLY to harvest one-line prose descriptions (display
text for the generated index) — never status/visibility (grill B7).
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from django.conf import settings
from django.db import transaction

from apps.brain.models import Entity, SyncRun
from apps.brain.services import gitrepo

log = logging.getLogger(__name__)

KNOWLEDGE_TYPES = {"takes": "take", "stories": "story", "lessons": "lesson", "facts": "fact"}

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
# INDEX line: `- [kind] name — description | ... | path`
_INDEX_LINE_RE = re.compile(r"^- \[[a-z]+\] (?P<name>[^—|]+)—\s*(?P<desc>[^|]+)")


@dataclass
class ParsedEntity:
    entity_id: str
    kind: str
    path: str  # repo-relative POSIX
    title: str = ""
    description: str = ""
    status: str = "current"
    superseded_by: str = ""
    visibility: str = "agents-only"
    topics: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    source: str = ""
    source_url: str = ""
    date: str = ""
    last_verified: object = None  # datetime.date | None
    content_hash: str = ""


def parse_frontmatter(text: str) -> tuple[dict, str, bool]:
    """(frontmatter, body, malformed).

    `malformed` is True only when a `---` block IS present but does not
    yield a YAML mapping. That distinction matters: a file with no block at
    all is legitimate (catalogs have none), but a block the author wrote
    and we could not read must not be treated as "no metadata" — see
    `_resolve_visibility`, which fails closed on it.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text, False
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text[m.end():], True
    if not isinstance(fm, dict):
        return {}, text[m.end():], True
    return fm, text[m.end():], False


def split_frontmatter(text: str) -> tuple[dict, str]:
    fm, body, _malformed = parse_frontmatter(text)
    return fm, body


def _title_of(body: str, fallback: str) -> str:
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else fallback


def as_list(v: object) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v.strip():
        return [s.strip() for s in v.strip("[]").split(",") if s.strip()]
    return []


def _parse_date(v: object):
    import datetime

    if isinstance(v, datetime.date):
        return v
    if isinstance(v, str):
        try:
            return datetime.date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contained_md(directory: Path, repo: Path) -> list[Path]:
    """Sorted `*.md` in `directory`, minus anything resolving out of `repo`.

    `glob` matches symlinks and `read_bytes()` follows them, so
    `identity/leak.md -> /data/state/boot-secrets.json` used to be read,
    parsed and published at whatever tier its frontmatter implied. `raw/`
    was never exposed this way — `snapshots._inside_raw()` resolves before
    accepting a path — but the five content globs never got the same
    treatment.

    Planting the link needs write access to the brain repo, so this is
    the operator or whoever has taken their git host. It is worth closing
    because the repo holds notes while the container holds the deploy
    key, the write PAT and the encryption key: "someone can write to my
    notes" must not escalate to "...and read the credentials that push to
    them".

    Skip and warn, never raise. One bad link must not stop the brain
    being served — and a dangling link used to do exactly that, crashing
    the sync with FileNotFoundError from `read_bytes()`.

    Containment, not a ban: a symlink resolving back inside the repo is
    ordinary content and is kept.
    """
    root = repo.resolve()
    kept: list[Path] = []
    for p in sorted(directory.glob("*.md")):
        try:
            resolved = p.resolve(strict=True)
        except OSError:
            log.warning(
                "indexer: skipping %s — broken symlink or unreadable path",
                p.name,
            )
            continue
        if root not in resolved.parents:
            log.warning(
                "indexer: skipping %s — it resolves outside the brain repo",
                p.name,
            )
            continue
        kept.append(p)
    return kept


def _harvest_index_descriptions(repo: Path) -> dict[str, str]:
    """path -> one-line description, cosmetic only (never status/visibility)."""
    out: dict[str, str] = {}
    index = repo / "INDEX.md"
    if not index.exists():
        return out
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("- ["):
            continue
        # The path is the last `|`-segment (with or without a label prefix).
        last = line.rsplit("|", 1)[-1].strip()
        path = last.split(":", 1)[-1].strip() if ":" in last else last
        m = _INDEX_LINE_RE.match(line)
        if path.endswith(".md") and m:
            out[path] = m.group("desc").strip()
    return out


def _resolve_visibility(explicit: object, default: str, *, malformed: bool = False) -> str:
    """Explicit frontmatter wins; otherwise the path default.

    When the frontmatter block exists but could not be parsed we cannot
    know what the author asked for, so we fall back to the MOST restrictive
    tier rather than the path default. Previously a YAML typo (or a UTF-8
    BOM, which stopped the block matching at all) silently turned a note
    marked `visibility: private` into the path default `agents-only` — and
    then copied it into the agents-only snapshot and served it. A file that
    fails to parse must never become more visible than it was.
    """
    if malformed:
        return "private"
    v = str(explicit).strip() if explicit else ""
    return v if v in {"public", "agents-only", "private"} else default


def parse_repo() -> list[ParsedEntity]:
    """Scan the clone and return every entity, per-kind parsed."""
    repo = gitrepo.repo_dir()
    descriptions = _harvest_index_descriptions(repo)
    entities: list[ParsedEntity] = []

    def read(p: Path) -> tuple[dict, str, str, bool]:
        raw = p.read_bytes()
        # utf-8-sig, not utf-8: a UTF-8 BOM (Notepad's default for years)
        # survived the decode and sat before the opening `---`, so
        # _FRONTMATTER_RE never matched and every field -- including
        # `visibility` -- was silently lost. content_hash still hashes the
        # raw bytes, BOM included.
        fm, body, malformed = parse_frontmatter(raw.decode("utf-8-sig", errors="replace"))
        if malformed:
            log.warning(
                "unparseable frontmatter in %s — indexing it as private",
                p.relative_to(repo).as_posix(),
            )
        return fm, body, _sha256(raw), malformed

    # --- knowledge notes ---
    for folder, kind in KNOWLEDGE_TYPES.items():
        for p in _contained_md(repo / "knowledge" / folder, repo):
            if p.name == "_TEMPLATE.md":
                continue
            fm, body, digest, malformed = read(p)
            rel = p.relative_to(repo).as_posix()
            entities.append(
                ParsedEntity(
                    entity_id=str(fm.get("id") or p.stem),
                    kind=str(fm.get("type") or kind),
                    path=rel,
                    title=_title_of(body, p.stem),
                    description=descriptions.get(rel, ""),
                    status=str(fm.get("status") or "current"),
                    superseded_by=str(fm.get("superseded_by") or "") if fm.get("superseded_by") else "",
                    visibility=_resolve_visibility(fm.get("visibility"), "agents-only", malformed=malformed),
                    topics=as_list(fm.get("topics")),
                    projects=as_list(fm.get("projects")),
                    source=str(fm.get("source") or ""),
                    source_url=str(fm.get("source_url") or ""),
                    date=str(fm.get("date") or ""),
                    content_hash=digest,
                )
            )

    # --- project cards ---
    for p in _contained_md(repo / "projects", repo):
        if p.name == "_TEMPLATE.md":
            continue
        fm, body, digest, malformed = read(p)
        rel = p.relative_to(repo).as_posix()
        entities.append(
            ParsedEntity(
                entity_id=str(fm.get("id") or f"project-{p.stem}"),
                kind="project",
                path=rel,
                title=_title_of(body, p.stem),
                description=descriptions.get(rel, ""),
                status="current",
                visibility=_resolve_visibility(fm.get("visibility"), "agents-only", malformed=malformed),
                topics=as_list(fm.get("topics")),
                last_verified=_parse_date(fm.get("last-verified")),
                content_hash=digest,
            )
        )

    # --- identity ---
    for p in _contained_md(repo / "identity", repo):
        if p.name == "_TEMPLATE.md":
            continue
        fm, body, digest, malformed = read(p)
        rel = p.relative_to(repo).as_posix()
        entities.append(
            ParsedEntity(
                entity_id=str(fm.get("id") or f"identity-{p.stem}"),
                kind="identity",
                path=rel,
                title=_title_of(body, p.stem),
                description=descriptions.get(rel, ""),
                visibility=_resolve_visibility(fm.get("visibility"), "agents-only", malformed=malformed),
                last_verified=_parse_date(fm.get("last-verified")),
                content_hash=digest,
            )
        )

    # --- lenses ---
    for p in _contained_md(repo / "lenses", repo):
        if p.name == "_TEMPLATE.md":
            continue
        fm, body, digest, malformed = read(p)
        rel = p.relative_to(repo).as_posix()
        entities.append(
            ParsedEntity(
                entity_id=f"lens-{fm.get('lens') or p.stem}",
                kind="lens",
                path=rel,
                title=_title_of(body, p.stem),
                description=descriptions.get(rel, ""),
                visibility=_resolve_visibility(fm.get("visibility"), "public", malformed=malformed),
                topics=as_list(fm.get("topics")),
                content_hash=digest,
            )
        )

    # --- catalogs (no frontmatter) ---
    for p in _contained_md(repo / "content-catalog", repo):
        if p.name in {"README.md", "_TEMPLATE.md"}:
            continue
        raw = p.read_bytes()
        body = raw.decode("utf-8", errors="replace")
        rel = p.relative_to(repo).as_posix()
        entities.append(
            ParsedEntity(
                entity_id=f"catalog-{p.stem}",
                kind="catalog",
                path=rel,
                title=_title_of(body, f"{p.stem} catalog"),
                description=descriptions.get(rel, ""),
                visibility="public",
                content_hash=_sha256(raw),
            )
        )

    return entities


def state_hash(entities: list[ParsedEntity] | None = None) -> str:
    """Deterministic hash of repo-parsed state — the drift comparator."""
    ents = entities if entities is not None else parse_repo()
    lines = sorted(f"{e.entity_id}\t{e.path}\t{e.content_hash}\t{e.visibility}\t{e.status}" for e in ents)
    return _sha256("\n".join(lines).encode())


def db_state_hash() -> str:
    rows = Entity.objects.values_list("entity_id", "path", "content_hash", "visibility", "status")
    lines = sorted(f"{a}\t{b}\t{c}\t{d}\t{e}" for a, b, c, d, e in rows)
    return _sha256("\n".join(lines).encode())


class DuplicateEntityId(Exception):
    """Two files in the brain repo claim the same frontmatter `id:`."""


#: Field limits read off the model so they cannot drift from the schema.
_MAX_LENGTHS: dict[str, int] = {
    f.name: f.max_length
    for f in Entity._meta.get_fields()
    if getattr(f, "max_length", None)
}
#: Truncating these is lossy but harmless. `path` and `entity_id` are
#: lookup keys — truncating them would break file reads or collide with
#: another note — so an over-long one skips the file instead.
_CLAMPABLE = ("title", "status", "superseded_by", "source", "source_url", "date", "kind")


def _fit_to_schema(parsed: list[ParsedEntity]) -> list[ParsedEntity]:
    """Clamp over-long frontmatter values instead of failing the whole sync.

    Postgres raises DataError on an over-length value and aborts the entire
    rebuild; SQLite (dev) accepts anything, so this only ever bit in
    production. A 300-char H1, `type: recommendation-notes` (kind is 16),
    `date: January 2026` (10), or a long tracking URL were each enough to
    freeze the index for every note in the repo.
    """
    kept: list[ParsedEntity] = []
    for e in parsed:
        too_long = [
            (f, _MAX_LENGTHS[f])
            for f in ("path", "entity_id")
            if len(getattr(e, f)) > _MAX_LENGTHS[f]
        ]
        if too_long:
            log.error(
                "skipping %s: %s exceeds the schema limit and cannot be truncated safely",
                e.path[:120],
                ", ".join(f"{f} ({_MAX_LENGTHS[f]})" for f, _ in too_long),
            )
            continue
        for f in _CLAMPABLE:
            limit = _MAX_LENGTHS.get(f)
            value = getattr(e, f)
            if limit and isinstance(value, str) and len(value) > limit:
                log.warning("%s: %s truncated to %d chars", e.path, f, limit)
                setattr(e, f, value[:limit])
        kept.append(e)
    return kept


def rebuild(trigger: str = "manual") -> SyncRun:
    """Full scan → prune → upsert. One SyncRun row per rebuild.

    Prune runs BEFORE the upsert, and the whole thing is one transaction.
    Both matter: `entity_id` and `path` are each unique, so renaming a note
    while keeping its frontmatter `id:` — the normal case, since `id` is
    the stable identity — used to hit the new path first and raise
    IntegrityError against the old row's `entity_id`. With no atomic block
    the rows written before the collision stayed committed, so the index
    was left half-migrated and every retry failed at the same file. Sync
    stayed dead until someone renamed the file back or edited the DB, and
    "Replace the clone" could not repair it because it re-runs this.
    """
    t0 = time.monotonic()
    run = SyncRun.objects.create(trigger=trigger)
    try:
        run.commit_sha = gitrepo.head_sha()
        parsed = _fit_to_schema(parse_repo())

        # A duplicate id is a repo authoring error, not a transient fault.
        # Report it as itself instead of letting the DB raise IntegrityError
        # halfway through the loop with no indication of which files clash.
        by_id: dict[str, list[str]] = {}
        for e in parsed:
            by_id.setdefault(e.entity_id, []).append(e.path)
        clashes = {k: v for k, v in by_id.items() if len(v) > 1}
        if clashes:
            detail = "; ".join(f"{k!r} in {' and '.join(sorted(v))}" for k, v in sorted(clashes.items()))
            raise DuplicateEntityId(f"duplicate frontmatter id: {detail}")

        added = changed = 0
        with transaction.atomic():
            seen_paths = {e.path for e in parsed}
            # Prune first so a renamed file's old row releases its unique
            # entity_id before the new path claims it.
            removed, _ = Entity.objects.exclude(path__in=seen_paths).delete()

            # Both unique columns can also collide without any rename: two
            # files swapping ids, or a file adopting an id a surviving row
            # still holds. Drop those rows so the upsert below re-creates
            # them cleanly instead of deadlocking on the constraint.
            parsed_by_path = {e.path: e for e in parsed}
            reidentified = {
                o.path
                for o in Entity.objects.all()
                if parsed_by_path[o.path].entity_id != o.entity_id
            }
            if reidentified:
                Entity.objects.filter(path__in=reidentified).delete()

            existing = {o.path: o for o in Entity.objects.all()}

            for e in parsed:
                values = dict(
                    entity_id=e.entity_id,
                    kind=e.kind,
                    title=e.title,
                    description=e.description,
                    status=e.status,
                    superseded_by=e.superseded_by,
                    visibility=e.visibility,
                    topics=e.topics,
                    projects=e.projects,
                    source=e.source,
                    source_url=e.source_url,
                    date=e.date,
                    last_verified=e.last_verified,
                    content_hash=e.content_hash,
                )
                obj = existing.get(e.path)
                if obj is None:
                    Entity.objects.create(path=e.path, **values)
                    # A row dropped just above for an id change is the same
                    # entity, not a new one — count it honestly.
                    if e.path in reidentified:
                        changed += 1
                    else:
                        added += 1
                    continue
                # Field-by-field, not content_hash: a parser change must still
                # repair rows whose file bytes didn't move (drift self-repair
                # rebuilds through here). Identical rows are skipped so `changed`
                # counts real changes, not upserts.
                dirty = [f for f, v in values.items() if getattr(obj, f) != v]
                if dirty:
                    for f in dirty:
                        setattr(obj, f, values[f])
                    obj.save(update_fields=dirty)
                    changed += 1
        run.added, run.changed, run.removed = added, changed, removed
        run.ok = True
    except Exception as exc:
        run.ok = False
        run.error = str(exc)
        raise
    finally:
        import django.utils.timezone as tz

        run.finished_at = tz.now()
        run.duration_ms = int((time.monotonic() - t0) * 1000)
        run.save()
    return run
