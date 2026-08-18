"""Ops view: the background-task monitor.

Every fire-and-forget click (extract, approve) hands work to the Q2
worker; this page is where that work is visible — running SDK
operations (`ok=None` rows exist BEFORE the run, grill C6), feeds with
an extraction in flight, approvals mid-commit, queue depth, and recent
outcomes. A run older than the Q2 task timeout is flagged stale: the
worker died before finalizing it.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from apps.brainconfig.nav import ops_context
from apps.feeds.models import Feed

from .models import Event, SdkOperation


@staff_member_required(login_url="login")
def tasks(request):
    stale_cutoff = timezone.now() - timedelta(seconds=int(settings.Q_CLUSTER["timeout"]) + 60)

    running = list(SdkOperation.objects.filter(ok__isnull=True)[:25])
    for op in running:
        op.is_stale = op.created_at < stale_cutoff

    extracting = [
        f
        for f in Feed.objects.filter(extract_queued_at__isnull=False)
        if f.extraction_in_flight
    ]
    approving = list(Feed.objects.filter(status="approving"))

    queue_depth = None
    try:
        from django_q.brokers import get_broker

        queue_depth = get_broker().queue_size()
    except Exception:  # broker down — the page still renders
        pass

    # Both ledgers grow without bound, so both paginate at 50. Separate
    # page params (`page` / `wpage`) because they live on one page; each
    # nav preserves the other's position. `worker_page` is None (not an
    # empty page) when django_q's table can't be read — the template
    # tells those two states apart.
    recent_page = Paginator(
        SdkOperation.objects.filter(ok__isnull=False), 50
    ).get_page(request.GET.get("page"))

    worker_page = None
    try:
        from django_q.models import Task

        worker_page = Paginator(Task.objects.order_by("-started"), 50).get_page(
            request.GET.get("wpage")
        )
    except Exception:
        pass

    # Ops jobs (the setup build, and the repair actions) are Q2 tasks like
    # any other, but django_q only materialises a Task row once one
    # FINISHES — so while they run the only trace here would be queue
    # depth. They publish their own progress records; read those, so
    # anything a button started is visible where everything else is.
    from apps.brainconfig import jobs as ops_jobs

    ops_job_rows = ops_jobs.active()

    live = [op for op in running if not op.is_stale]
    busy = bool(
        live or extracting or approving
        or any(j.get("state") == "running" for j in ops_job_rows)
    )
    return render(
        request,
        "ops/tasks.html",
        {
            "running": running,
            "recent_page": recent_page,
            "extracting": extracting,
            "approving": approving,
            "queue_depth": queue_depth,
            "worker_page": worker_page,
            "worker_page_num": worker_page.number if worker_page else 1,
            "ops_jobs": ops_job_rows,
            "busy": busy,
            **ops_context(request),
        },
    )


ACTIVITY_LIMIT = 100


@staff_member_required(login_url="login")
def activity_json(request):
    """M3.5.4 — the tail of the read log, for the live overlay.

    A cursor, not a window: `?after=<id>` returns only what has happened
    since, so a visual can poll cheaply and never replay. Called without
    a cursor it returns no events and just hands back the current
    `last_id`, so a page that loads doesn't fire a burst of pulses for
    reads that happened yesterday.

    Polling, not SSE: an ops page that idles all day shouldn't hold a
    streaming connection open, and a missed poll costs nothing — the
    cursor picks up every event on the next tick.
    """
    try:
        after = int(request.GET.get("after") or 0)
    except ValueError:
        after = 0

    latest = Event.objects.filter(type="read").order_by("-id").values_list("id", flat=True).first()
    events = []
    if after:
        rows = Event.objects.filter(type="read", id__gt=after).select_related("consumer").order_by("id")
        events = [
            {
                "id": e.id,
                "at": e.created_at.isoformat(timespec="seconds"),
                "entity_ids": e.entity_ids or [],
                "endpoint": (e.details or {}).get("endpoint", ""),
                "tier": (e.details or {}).get("tier", ""),
                # A NULL consumer is only "ops" when nothing else claims
                # the call: connector reads can't sit in the FK (it's an
                # APIKey FK) and carry a `via` label instead.
                "consumer": (
                    str(e.consumer)
                    if e.consumer
                    else ((e.details or {}).get("via") or "ops")
                ),
            }
            for e in rows[:ACTIVITY_LIMIT]
        ]
    return JsonResponse({"last_id": events[-1]["id"] if events else (latest or 0), "events": events})


DAY_CHOICES = (1, 7, 30, 90)
LOGS_PAGE_SIZE = 50


@staff_member_required(login_url="login")
def logs(request):
    """M3.4 — the observability layer: event stream with filters, the
    SdkOperation ledger aggregated by kind, most-served entities, and
    spend-vs-cap with the breaker state. Aggregates use plain ORM sums
    so they reconcile with raw queries by construction (the M3.4 check
    verifies exactly that). The stream pages at 50 (M5.6 polish): an
    event log grows without bound, and a flat newest-200 cap silently
    hid everything older."""
    from apps.brainconfig import services as config
    from apps.reader.services.sdk_runner import today_cost_usd, today_unpriced

    try:
        days = int(request.GET.get("days") or 7)
    except ValueError:
        days = 7
    if days not in DAY_CHOICES:
        days = 7
    etype = request.GET.get("type", "")
    since = timezone.now() - timedelta(days=days)

    events_qs = Event.objects.select_related("consumer").filter(created_at__gte=since)
    if etype:
        events_qs = events_qs.filter(type=etype)

    # Page AFTER filtering (the browser pattern): the filter form carries
    # no `page` input, so changing a filter lands back on page 1, and
    # get_page clamps junk and out-of-range values instead of 404ing.
    paginator = Paginator(events_qs, LOGS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    # `credential` is resolved HERE, not in the template. The template
    # had `{{ row.e.consumer|default:row.e.details.via|default:"—" }}`,
    # and Django resolves a filter ARGUMENT strictly: a missing key
    # raises VariableDoesNotExist instead of rendering empty, the way a
    # missing `{{ variable }}` does. Most event types carry no `via` at
    # all — `settings_change`, `csp_violation`, `endpoint_error` — so one
    # of those anywhere in the window 500'd the whole page. Same
    # expression as `activity.json` above, which never had the problem
    # because it does the lookup in Python.
    event_rows = [
        {
            "e": e,
            "details_json": json.dumps(e.details, ensure_ascii=False, default=str)[:300],
            "n_entities": len(e.entity_ids or []),
            "credential": (
                str(e.consumer) if e.consumer else ((e.details or {}).get("via") or "")
            ),
        }
        for e in page_obj
    ]
    event_types = sorted(set(Event.objects.values_list("type", flat=True).distinct()))

    # The filters re-encoded WITHOUT `page`, so pager links compose as
    # "?{filter_qs}&page=N" and never carry a stale page number. `days`
    # is always present, so the template joins unconditionally.
    filter_qs = urlencode({"days": days, **({"type": etype} if etype else {})})

    ops = SdkOperation.objects.filter(created_at__gte=since)
    agg = dict(
        runs=Count("id"),
        errors=Count("id", filter=Q(ok=False)),
        running=Count("id", filter=Q(ok__isnull=True)),
        tin=Sum("input_tokens"),
        tout=Sum("output_tokens"),
        cost=Sum("cost_usd"),
    )
    by_kind = list(ops.values("kind").annotate(**agg).order_by("-cost"))
    totals = ops.aggregate(**agg)

    served = Counter()
    for ids in Event.objects.filter(type="read", created_at__gte=since).values_list(
        "entity_ids", flat=True
    ):
        served.update(ids or [])

    today_cost = today_cost_usd()
    cap = config.daily_cost_cap()  # None = breaker disabled (cap 0)
    cap_pct = min(100, round(today_cost / float(cap) * 100)) if cap else None
    # Runs that burned tokens but never reported a cost (timeout, client
    # disconnect). Real spend the cap cannot see — shown rather than
    # priced, because a made-up dollar figure inside a breaker is worse
    # than an admitted blind spot.
    unpriced = today_unpriced()

    return render(
        request,
        "ops/logs.html",
        {
            "days": days,
            "day_choices": DAY_CHOICES,
            "etype": etype,
            "event_types": event_types,
            "event_rows": event_rows,
            "page_obj": page_obj,
            "filter_qs": filter_qs,
            "by_kind": by_kind,
            "totals": totals,
            "most_served": served.most_common(10),
            "today_cost": today_cost,
            "cap": cap,
            "cap_pct": cap_pct,
            "unpriced": unpriced,
            **ops_context(request),
        },
    )
