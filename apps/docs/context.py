"""Shared template context for `/docs/` pages.

Mirror of `apps.dashboard.context` and `apps.admin_panel.context` —
builds the sidebar nav (endpoints grouped by tag + the static guides
section) + the topbar's `nav_links` slot.

Each docs view does:

    ctx = build_layout_context(request, active="endpoint:hello")
    ctx["something_specific"] = ...
    return render(request, "docs/endpoint_detail.html", ctx)

`active` is a slug-prefixed string (`endpoint:<slug>` for endpoint
detail pages, `guide:<slug>` for static guides, `index` for the
catalog). The matching nav item gets `aria-current="page"` styling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.urls import reverse


@dataclass(frozen=True)
class GuideEntry:
    slug: str
    label: str


# The 5 static guides shipped in 10.4.7. Adding a new guide = one new
# row here + one new `apps/docs/guides/<slug>.md` file.
GUIDES: list[GuideEntry] = [
    GuideEntry("auth", "Authentication"),
    GuideEntry("mcp-setup", "MCP setup"),
    GuideEntry("rate-limits", "Rate limits"),
    GuideEntry("webhooks", "Webhooks"),
    GuideEntry("errors", "Error codes"),
]


def _safe_url(url_name: str, **kwargs) -> str:
    """Resolve `url_name`, falling back to "#" if it can't be reversed.
    Defensive guard so a missing route degrades the sidebar link rather
    than 500ing the whole page."""
    try:
        return reverse(url_name, kwargs=kwargs) if kwargs else reverse(url_name)
    except Exception:
        return "#"


def _endpoint_nav_section(active: str) -> dict[str, Any]:
    """Build the "Endpoints" section by walking the registry. One
    flat list ordered by slug for v1; tag grouping happens on the
    catalog page itself (sidebar stays flat to keep navigation
    fast)."""
    items: list[dict[str, Any]] = []
    try:
        from apps.core.registry import registry

        for spec in registry.all():
            slug_marker = f"endpoint:{spec.slug}"
            items.append(
                {
                    "label": spec.slug,
                    "url": _safe_url("docs:endpoint-detail", slug=spec.slug),
                    "icon": "list",
                    "active": slug_marker == active,
                    "badge": 0,
                }
            )
    except Exception:
        # Registry import failure → leave the section empty rather
        # than blowing up the page.
        pass
    return {"label": "Endpoints", "items": items}


def _guides_nav_section(active: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for g in GUIDES:
        slug_marker = f"guide:{g.slug}"
        items.append(
            {
                "label": g.label,
                "url": _safe_url("docs:guide", slug=g.slug),
                "icon": "book",
                "active": slug_marker == active,
                "badge": 0,
            }
        )
    return {"label": "Guides", "items": items}


def _server_nav_section() -> dict[str, Any]:
    """The cross-app pivot back to the ops UI.

    Mirrors the ops sidebar's "Public" section, which points the other
    way. It lived in the topbar until the docs topbar went mobile-only —
    on desktop that bar held this one link, so the link moved rather
    than disappearing.

    Staff-only, which is what `build_top_links` always claimed ("the
    partial hides the link when the user is anon") and the topbar
    partial never actually did: /docs/ is public, and an anonymous
    reader following this only reaches a login redirect.
    """
    return {
        "label": "Server",
        "items": [
            {
                "label": "Dashboard",
                # `brainconfig:dashboard`, NOT `dashboard` — the ops
                # urlconf sets app_name, so the bare name has never
                # reversed. `_safe_url` swallowed the NoReverseMatch and
                # returned "#", so the link rendered and did nothing;
                # it was already dead in the topbar this moved out of.
                "url": _safe_url("brainconfig:dashboard"),
                "icon": "dashboard",
                "active": False,
                "badge": 0,
            },
        ],
    }


def build_nav_sections(
    active: str,
    *,
    is_staff: bool = False,
) -> list[dict[str, Any]]:
    sections = [
        {
            "label": "",
            "items": [
                {
                    "label": "All endpoints",
                    "url": _safe_url("docs:index"),
                    "icon": "home",
                    "active": active == "index",
                    "badge": 0,
                },
            ],
        },
        _endpoint_nav_section(active),
        _guides_nav_section(active),
    ]
    if is_staff:
        sections.append(_server_nav_section())
    return sections


def build_top_links(*, is_staff: bool = False) -> list[dict[str, Any]]:
    """Topbar nav links — the mobile-only bar's copy of the ops pivot.

    The old docstring here claimed "the partial hides the link when the
    user is anon"; it never did, so every public visitor was offered a
    link that only 302s to the login page. Gating happens here now, and
    matches the sidebar's "Server" section so the same visitor sees the
    same nav on both sides of the `lg` breakpoint."""
    if not is_staff:
        return []
    return [
        {"label": "Dashboard", "url": _safe_url("brainconfig:dashboard"), "active": False},
    ]


def build_layout_context(request, *, active: str) -> dict[str, Any]:
    user = getattr(request, "user", None)
    is_staff = bool(user is not None and user.is_authenticated and user.is_staff)
    return {
        "docs_nav_sections": build_nav_sections(active, is_staff=is_staff),
        "docs_top_links": build_top_links(is_staff=is_staff),
        "active_section": active,
    }


__all__ = [
    "GUIDES",
    "GuideEntry",
    "build_layout_context",
    "build_nav_sections",
    "build_top_links",
]
