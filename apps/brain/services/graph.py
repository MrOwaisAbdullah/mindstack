"""M3.5.1 — the brain as a graph, built off Entity + Events.

One data source, several views (PLAN.md §11 M3.5): the visibility rings
(3.5.2), the graph explorer (3.5.3), the activity overlay (3.5.4) and the
belief timeline (3.5.5) all read this payload, so the shapes they draw
can never disagree about what the brain contains.

Nodes are of two types:

- `entity` — one per Entity row: its kind, resolved tier, status,
  staleness, and how often it was actually served (`read` events).
- `topic`  — one per topic in use. Topics aren't files in the repo, so
  they exist only as hubs here. Hub nodes keep topic membership linear
  (one edge per entity-topic pair) instead of the ~600-edge hairball a
  pairwise encoding would produce, and they give the force layout in
  3.5.3 something real to cluster around.

Edges are entity→entity except for `topic`:

| type | meaning |
|---|---|
| `topic` | entity carries this topic (entity → topic hub) |
| `project` | note touches this project card (`projects:` → the card) |
| `superseded` | old note → the note that replaced it (`superseded_by:`) |
| `source` | two entities compiled from the same source id |

Nothing here filters by visibility: this is the ops view, which is
operator-only behind the network boundary and deliberately sees all three
tiers — the rings are *about* the tiers. A public showcase mode would
have to build a tier-filtered payload (post-M5, PLAN.md §11).
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from itertools import combinations

from django.urls import reverse
from django.utils import timezone

from apps.brain.models import Entity
from apps.events.models import Event

from . import gitrepo
from .indexer import as_list, split_frontmatter
from .snapshots import TIER_ORDER
from .staleness import STALE_AFTER_DAYS, is_stale

DEFAULT_WINDOW_DAYS = 30

# Above this cluster size, `source` edges become a star (anchor = the
# lowest entity id) instead of a full mesh: a source shared by n notes
# would otherwise contribute n(n-1)/2 edges on its own. Today's biggest
# cluster is 4, so this is a guard, not a live behaviour.
SOURCE_MESH_MAX = 12


def read_counts(days: int | None = DEFAULT_WINDOW_DAYS) -> Counter:
    """How often each entity id appears in a `read` event.

    `days=None` (or 0) means all time. Ids that aren't entities —
    `get-index` logs "INDEX", `get-raw` logs a repo path — stay in the
    Counter and are simply never looked up.
    """
    qs = Event.objects.filter(type="read")
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - dt.timedelta(days=days))
    counts: Counter = Counter()
    for ids in qs.values_list("entity_ids", flat=True):
        counts.update(ids or [])
    return counts


def _project_index(entities: list[Entity]) -> dict[str, str]:
    """Map how notes name projects → the project card's entity id.

    Notes write the bare slug (`projects: [pyrunner]`); the card indexes
    as `project-pyrunner` (indexer.py falls back to `project-<stem>`).
    Both spellings resolve, so a card that declares its own `id:` keeps
    working too.
    """
    index: dict[str, str] = {}
    for e in entities:
        if e.kind != "project":
            continue
        stem = e.path.rsplit("/", 1)[-1].removesuffix(".md")
        index[e.entity_id] = e.entity_id
        index.setdefault(stem, e.entity_id)
    return index


# A lens's `types:` field scopes *note* types ("note types: all" in the
# lens contract), so it is applied to knowledge notes only — it must not
# quietly drop project cards or catalogs.
KNOWLEDGE_KINDS = frozenset({"take", "story", "lesson", "fact"})


def _within(ceiling: str, visibility: str) -> bool:
    return TIER_ORDER.get(visibility, 2) <= TIER_ORDER.get(ceiling, 0)


def lens_definitions(entities: list[Entity]) -> list[dict]:
    """Resolve each lens to the exact set of entities it scopes.

    A lens is a named retrieval scope, and its full definition lives in
    frontmatter that the Entity row doesn't carry (`types`,
    `visibility-ceiling`, `identity`) — so it's read from the clone, the
    same canonical copy the browser renders notes from.

    Membership, in the lens's own terms:
    - never above the lens's `visibility-ceiling`;
    - the identity files it names explicitly (`identity: [core, beliefs]`
      → `identity-core`, `identity-beliefs`) — those carry no topics and
      would otherwise never match;
    - otherwise: shares at least one topic with the lens, and (for
      knowledge notes) has a type the lens admits.

    Resolving this server-side means the explorer's highlight and any
    future reader-side lens filter are the same computation, not two
    implementations that can drift.
    """
    repo = gitrepo.repo_dir()
    out: list[dict] = []

    for lens in [e for e in entities if e.kind == "lens"]:
        fm: dict = {}
        error = ""
        try:
            fm, _body = split_frontmatter((repo / lens.path).read_text(encoding="utf-8"))
        except OSError as exc:
            error = f"{exc.__class__.__name__}: could not read {lens.path}"

        topics = set(as_list(fm.get("topics")) or list(lens.topics or []))
        types = {t.lower() for t in as_list(fm.get("types"))}
        ceiling = str(fm.get("visibility-ceiling") or "private").strip()
        identity = as_list(fm.get("identity"))
        # Lens files name identity files bare (`core`), the index keys
        # them `identity-core`; accept either spelling.
        identity_ids = {f"identity-{n}" for n in identity} | set(identity)

        def member(e: Entity) -> bool:
            if e.entity_id == lens.entity_id or not _within(ceiling, e.visibility):
                return False
            if e.entity_id in identity_ids:
                return True
            if not topics or not (set(e.topics or []) & topics):
                return False
            if types and "all" not in types and e.kind in KNOWLEDGE_KINDS:
                return e.kind in types
            return True

        members = [e.entity_id for e in entities if member(e)]
        out.append(
            {
                "id": lens.entity_id,
                "name": lens.entity_id.removeprefix("lens-"),
                "title": lens.title or lens.entity_id,
                "url": reverse("brainconfig:entity", args=[lens.entity_id]),
                "topics": sorted(topics),
                "types": sorted(types) or ["all"],
                "ceiling": ceiling,
                "identity": sorted(identity_ids & {e.entity_id for e in entities}),
                "members": members,
                "member_count": len(members),
                "error": error,
            }
        )
    return out


def build_graph(*, days: int | None = DEFAULT_WINDOW_DAYS) -> dict:
    entities = list(Entity.objects.all().order_by("kind", "entity_id"))
    known = {e.entity_id for e in entities}
    reads = read_counts(days)
    projects = _project_index(entities)

    nodes: list[dict] = []
    edges: list[dict] = []
    topic_members: dict[str, int] = defaultdict(int)
    by_source: dict[str, list[str]] = defaultdict(list)

    for e in entities:
        nodes.append(
            {
                "id": e.entity_id,
                "type": "entity",
                "kind": e.kind,
                "tier": e.visibility,
                "status": e.status,
                "current": e.is_current,
                "stale": is_stale(e),
                "title": e.title or e.entity_id,
                "path": e.path,
                "date": e.date,
                "topics": list(e.topics or []),
                "projects": list(e.projects or []),
                "source": e.source,
                "reads": reads.get(e.entity_id, 0),
                "url": reverse("brainconfig:entity", args=[e.entity_id]),
            }
        )

        for topic in e.topics or []:
            topic_members[topic] += 1
            edges.append({"source": e.entity_id, "target": f"topic:{topic}", "type": "topic"})

        for slug in e.projects or []:
            card = projects.get(slug)
            if card and card != e.entity_id:
                edges.append({"source": e.entity_id, "target": card, "type": "project"})

        # Supersede chains: only ever point at a note that really exists,
        # so a dangling `superseded_by:` can't invent a node.
        if e.superseded_by and e.superseded_by in known:
            edges.append({"source": e.entity_id, "target": e.superseded_by, "type": "superseded"})

        if e.source:
            by_source[e.source].append(e.entity_id)

    for topic, size in sorted(topic_members.items()):
        nodes.append(
            {
                "id": f"topic:{topic}",
                "type": "topic",
                "kind": "topic",
                "label": topic,
                "size": size,
            }
        )

    starred = 0
    for source, members in sorted(by_source.items()):
        if len(members) < 2:
            continue
        members = sorted(members)
        if len(members) > SOURCE_MESH_MAX:
            starred += 1
            pairs = ((members[0], other) for other in members[1:])
        else:
            pairs = combinations(members, 2)
        for a, b in pairs:
            edges.append({"source": a, "target": b, "type": "source", "via": source})

    edge_counts: Counter = Counter(edge["type"] for edge in edges)
    lenses = lens_definitions(entities)
    return {
        "generated_at": timezone.now().isoformat(timespec="seconds"),
        "window_days": days or None,
        "stale_after_days": STALE_AFTER_DAYS,
        "tiers": ["private", "agents-only", "public"],
        "kinds": [k for k, _ in Entity.KINDS],
        "counts": {
            "nodes": len(nodes),
            "entities": len(entities),
            "topics": len(topic_members),
            "edges": {**edge_counts, "total": len(edges)},
            "by_kind": dict(Counter(e.kind for e in entities)),
            "by_tier": dict(Counter(e.visibility for e in entities)),
            "reads_counted": sum(reads.get(e.entity_id, 0) for e in entities),
            "source_clusters_starred": starred,
            "lenses": len(lenses),
        },
        "lenses": lenses,
        "nodes": nodes,
        "edges": edges,
    }
