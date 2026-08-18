"""Ops UI: dashboard v1 + brain browser (PLAN.md §8, step M1.10).

Staff-only pages over the Entity index and event/ledger tables. The
browser reads note bodies from the server's clone (full private view —
this UI is operator-only, behind the network boundary); consumers never see
these pages, they get tier snapshots via the API.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from urllib.parse import urlencode

import markdown as md
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from apps.brainconfig.nav import ops_context
from apps.events.models import Event, SdkOperation
from apps.feeds.models import Feed

from .models import Entity, SyncRun
from .services import gitrepo
from .services.graph import DEFAULT_WINDOW_DAYS, build_graph
from .services.staleness import STALE_AFTER_DAYS, is_stale as _is_stale


@staff_member_required(login_url="login")
def dashboard(request):
    now = timezone.now()
    day_ago = now - dt.timedelta(days=1)
    week_ago = now - dt.timedelta(days=7)

    last_sync = SyncRun.objects.first()
    probe = gitrepo.status_probe()

    entities = Entity.objects.all()
    kind_counts = list(
        entities.values("kind").annotate(n=Count("id")).order_by("-n")
    )
    vis_counts = {
        row["visibility"]: row["n"]
        for row in entities.values("visibility").annotate(n=Count("id"))
    }
    superseded = entities.filter(status="superseded").count()

    stale_projects = sorted(
        (e for e in entities.filter(kind="project") if _is_stale(e)),
        key=lambda e: (e.last_verified or dt.date.min),
    )

    reads_day = Event.objects.filter(type="read", created_at__gte=day_ago).count()
    reads_week = Event.objects.filter(type="read", created_at__gte=week_ago).count()

    # 14-day read counts, pre-baked into SVG polyline points (a 100×28
    # viewBox, 1px padding) — the template can't do arithmetic and the
    # CSP forbids style= widths, so geometry is computed here.
    spark_days = 14
    spark_start = (now - dt.timedelta(days=spark_days - 1)).date()
    per_day = {
        row["d"]: row["n"]
        for row in Event.objects.filter(
            type="read", created_at__date__gte=spark_start
        )
        .annotate(d=TruncDate("created_at"))
        .values("d")
        .annotate(n=Count("id"))
    }
    series = [
        per_day.get(spark_start + dt.timedelta(days=i), 0)
        for i in range(spark_days)
    ]
    peak = max(series) or 1
    reads_spark = " ".join(
        f"{i * 100 / (spark_days - 1):.1f},{27 - (v / peak) * 24:.1f}"
        for i, v in enumerate(series)
    )

    served = Counter()
    for ids in Event.objects.filter(
        type="read", created_at__gte=now - dt.timedelta(days=30)
    ).values_list("entity_ids", flat=True):
        served.update(ids or [])
    most_served = served.most_common(8)

    def spend(since):
        qs = SdkOperation.objects.filter(created_at__gte=since)
        agg = qs.aggregate(
            cost=Sum("cost_usd"), tin=Sum("input_tokens"), tout=Sum("output_tokens")
        )
        return {
            "runs": qs.count(),
            "errors": qs.filter(ok=False).count(),
            "cost": agg["cost"] or 0,
            "tokens_in": agg["tin"] or 0,
            "tokens_out": agg["tout"] or 0,
        }

    # Setup health, top of the page. Nobody reads a docs page after the
    # thing is already running, so the remaining setup work has to be
    # visible state — above all "your ops UI is on the public internet".
    from apps.brainconfig import health as health_checks

    health_problems = health_checks.problems(health_checks.all_checks(request))

    # A settings page saved six times is one fact, not six rows — collapse
    # consecutive same-type/same-consumer events into one entry carrying
    # the newest timestamp and a count. Fetch deep so a chatty burst
    # can't push everything else off the card.
    recent_events = []
    for ev in Event.objects.select_related("consumer")[:40]:
        # `via` is the connector label for credentials the consumer FK
        # can't hold; in the key it keeps two different connectors from
        # collapsing into one line.
        via = (ev.details or {}).get("via", "")
        key = (ev.type, ev.consumer_id, via)
        if recent_events and recent_events[-1]["key"] == key:
            recent_events[-1]["count"] += 1
            continue
        recent_events.append(
            {
                "key": key,
                "label": ev.type.replace("_", " "),
                "consumer": ev.consumer if ev.consumer_id else (via or None),
                "at": ev.created_at,
                "count": 1,
            }
        )
        if len(recent_events) >= 10:
            break

    return render(
        request,
        "ops/dashboard.html",
        {
            "health_problems": health_problems,
            "last_sync": last_sync,
            "probe": probe,
            "entity_total": entities.count(),
            "kind_counts": kind_counts,
            "vis_public": vis_counts.get("public", 0),
            "vis_agents": vis_counts.get("agents-only", 0),
            "vis_private": vis_counts.get("private", 0),
            "superseded": superseded,
            "stale_projects": stale_projects,
            "stale_after": STALE_AFTER_DAYS,
            "reads_day": reads_day,
            "reads_week": reads_week,
            "reads_spark": reads_spark,
            "most_served": most_served,
            "spend_day": spend(day_ago),
            "spend_week": spend(week_ago),
            "pending_feeds": Feed.objects.filter(status="pending").count(),
            "recent_events": recent_events,
            "recent_ops": SdkOperation.objects.all()[:5],
            **ops_context(request),
        },
    )


@staff_member_required(login_url="login")
def graph_json(request):
    """M3.5.1 — the shared data source for every brain visual.

    `?days=N` scopes the read counts (default 30); `?days=0` means all
    time. Staff-only and un-cached: it is a live view of the index, and
    it deliberately carries all three tiers (see services/graph.py).
    """
    try:
        days = int(request.GET.get("days", DEFAULT_WINDOW_DAYS))
    except ValueError:
        days = DEFAULT_WINDOW_DAYS
    days = max(0, min(days, 3650)) or None
    return JsonResponse(build_graph(days=days))


@staff_member_required(login_url="login")
def graph_explorer(request):
    """M3.5.3 — force layout over the whole brain, lens highlighting.

    The page is a shell: all of it is drawn client-side from
    graph.json, so there is exactly one server-side definition of what
    the brain contains and what a lens scopes.
    """
    return render(request, "ops/graph.html", ops_context(request))


@staff_member_required(login_url="login")
def timeline(request):
    """M3.5.5 — growth by month + every supersede chain, over graph.json."""
    return render(request, "ops/timeline.html", ops_context(request))


@staff_member_required(login_url="login")
def browser(request):
    qs = Entity.objects.all().order_by("kind", "entity_id")
    kind = request.GET.get("kind", "")
    visibility = request.GET.get("visibility", "")
    status = request.GET.get("status", "")
    q = (request.GET.get("q") or "").strip()

    if kind:
        qs = qs.filter(kind=kind)
    if visibility:
        qs = qs.filter(visibility=visibility)
    if status == "superseded":
        qs = qs.filter(status="superseded")
    elif status == "current":
        qs = qs.exclude(status="superseded")
    if q:
        from django.db.models import Q

        qs = qs.filter(
            Q(entity_id__icontains=q)
            | Q(title__icontains=q)
            | Q(topics__icontains=q)
            | Q(projects__icontains=q)
        )

    # Page AFTER filtering; the filter form carries no `page` input, so
    # changing a filter naturally lands back on page 1. get_page clamps
    # junk and out-of-range values instead of 404ing.
    paginator = Paginator(qs, BROWSER_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    rows = [{"e": e, "stale": _is_stale(e)} for e in page_obj]

    # The filters re-encoded WITHOUT `page`, so pager links compose as
    # "?{filter_qs}&page=N" and never carry a stale page number.
    filter_qs = urlencode(
        {
            k: v
            for k, v in (
                ("kind", kind),
                ("visibility", visibility),
                ("status", status),
                ("q", q),
            )
            if v
        }
    )

    return render(
        request,
        "ops/browser.html",
        {
            "rows": rows,
            "page_obj": page_obj,
            "filter_qs": filter_qs,
            "total": Entity.objects.count(),
            "kinds": [k for k, _ in Entity.KINDS],
            "visibilities": [v for v, _ in Entity.VISIBILITIES],
            "f_kind": kind,
            "f_visibility": visibility,
            "f_status": status,
            "f_q": q,
            **ops_context(request),
        },
    )


# Browser page size. 50 keeps the page fast at a few thousand notes
# while a fresh brain (dozens of entities) never sees a pager at all.
BROWSER_PAGE_SIZE = 50

_FRONTMATTER_END = "\n---"


def _split_frontmatter(raw: str) -> tuple[str, str]:
    if not raw.startswith("---"):
        return "", raw
    end = raw.find(_FRONTMATTER_END, 3)
    if end == -1:
        return "", raw
    return raw[3:end].strip(), raw[end + len(_FRONTMATTER_END):].lstrip("-\n")


@staff_member_required(login_url="login")
def entity_detail(request, entity_id: str):
    try:
        entity = Entity.objects.get(entity_id=entity_id)
    except Entity.DoesNotExist:
        raise Http404(entity_id)

    path = gitrepo.repo_dir() / entity.path
    frontmatter, body_html, read_error = "", "", ""
    try:
        raw = path.read_text(encoding="utf-8")
        frontmatter, body = _split_frontmatter(raw)
        body_html = md.markdown(body, extensions=["extra", "sane_lists"])
    except OSError as exc:
        read_error = f"{exc.__class__.__name__}: could not read {entity.path}"

    superseded_by = None
    if entity.superseded_by:
        superseded_by = Entity.objects.filter(entity_id=entity.superseded_by).first()

    return render(
        request,
        "ops/entity.html",
        {
            "e": entity,
            "stale": _is_stale(entity),
            "stale_after": STALE_AFTER_DAYS,
            "frontmatter": frontmatter,
            "body_html": body_html,
            "read_error": read_error,
            "superseded_by": superseded_by,
            **ops_context(request),
        },
    )
