"""Docs site views.

Staff-only since the nothing-public decision (2026-07-28): every view
requires a logged-in staff session, and in prod the whole `/docs/`
prefix additionally sits behind the IP-allowlist perimeter
(IP_ALLOWLIST_EXTRA_PREFIXES) so outsiders get a 404, never a login
redirect. The only public pages are the landing page, /api/, /mcp,
the webhook, and health checks.
"""
from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.docs import context
from apps.docs.services import catalog as catalog_service
from apps.docs.services import endpoint_detail as detail_service
from apps.docs.services import guides as guides_service


@staff_member_required(login_url="login")
@require_GET
def index_view(request: HttpRequest) -> HttpResponse:
    """Endpoint catalog.

    Lists every registered endpoint grouped by tag with a client-side
    Alpine search filter. Tag groups are alphabetical with
    `Uncategorized` last; cards inside each group sort by slug.
    """
    bundle = catalog_service.get_catalog()
    ctx = context.build_layout_context(request, active="index")
    ctx.update({"bundle": bundle})
    return render(request, "docs/index.html", ctx)


@staff_member_required(login_url="login")
@require_GET
def endpoint_detail_view(request: HttpRequest, slug: str) -> HttpResponse:
    """Per-endpoint detail page (10.4.3 / 10.4.4 / 10.4.5).

    Header (verb / path / version / deprecated pill) +
    description + 4 tabs (REST / MCP / Python SDK / JS SDK) +
    Pydantic-rendered Input + Output schema tables with collapsible
    nested sub-schemas + try-it placeholder for G4.

    Resolves the spec via `apps.core.registry.registry.by_slug(slug)` —
    404 on unknown slug. v1 is the default; multi-version slugs pick
    the v1 row (per-version detail pages can land in Phase 11 if
    needed; today no endpoint ships a non-v1 alternate).
    """
    from apps.core.registry import registry

    matches = registry.by_slug(slug)
    if not matches:
        raise Http404(f"Endpoint {slug!r} not registered.")
    spec = next((s for s in matches if s.version == "v1"), matches[0])

    bundle = detail_service.get_endpoint_detail(spec, request)
    ctx = context.build_layout_context(request, active=f"endpoint:{slug}")
    ctx.update({"bundle": bundle, "spec": spec})
    return render(request, "docs/endpoint_detail.html", ctx)


@staff_member_required(login_url="login")
@require_GET
def guide_view(request: HttpRequest, slug: str) -> HttpResponse:
    """Static guide.

    Slug is whitelisted against `context.GUIDES` so a crafted URL with
    a path-traversal-like slug (`../foo`) can't read arbitrary files
    from disk. 404 on (a) unknown slug, (b) markdown file missing
    from `apps/docs/guides/`.
    """
    entry = next((g for g in context.GUIDES if g.slug == slug), None)
    if entry is None:
        raise Http404(f"Guide {slug!r} not registered.")
    bundle = guides_service.render_guide(slug, title=entry.label)
    if bundle is None:
        raise Http404(f"Guide {slug!r} markdown file missing.")
    ctx = context.build_layout_context(request, active=f"guide:{slug}")
    ctx.update({"bundle": bundle})
    return render(request, "docs/guide.html", ctx)
