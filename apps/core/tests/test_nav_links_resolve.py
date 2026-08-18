"""Every nav link points somewhere. A `#` means a route name went stale.

`apps.docs.context._safe_url` swallows NoReverseMatch on purpose, so a
missing route degrades one sidebar row instead of 500ing the page. The
price is that a WRONG route name is invisible: the docs "Dashboard"
pivot asked for `dashboard` when the ops urlconf sets
`app_name = "brainconfig"`, so it reversed to "#" and rendered as a
link that went nowhere. It sat like that in the topbar, survived the
move into the sidebar, and was caught by a human clicking it.

DB-free on purpose (see CLAUDE.md): the nav builders only reverse URLs
and read `request.user` flags.
"""
from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse

from apps.brainconfig.nav import ops_context
from apps.docs.context import build_layout_context


class _StaffUser:
    """Enough user for the nav builders; no DB row needed."""

    is_authenticated = True
    is_staff = True


def _urls(sections) -> list[tuple[str, str]]:
    return [
        (item["label"], item["url"])
        for section in sections
        for item in section["items"]
    ]


def _staff_request(path: str):
    request = RequestFactory().get(path)
    request.user = _StaffUser()
    return request


def test_ops_nav_has_no_dead_links():
    rows = _urls(ops_context(RequestFactory().get("/ops/"))["ops_nav_sections"])
    assert rows, "ops nav built empty"
    dead = [label for label, url in rows if not url or url == "#"]
    assert not dead, f"ops nav links that reverse to '#': {dead}"


def test_docs_nav_has_no_dead_links():
    ctx = build_layout_context(_staff_request("/docs/"), active="index")
    rows = _urls(ctx["docs_nav_sections"])
    assert rows, "docs nav built empty"
    dead = [label for label, url in rows if not url or url == "#"]
    assert not dead, f"docs nav links that reverse to '#': {dead}"


def test_brand_url_takes_staff_to_the_dashboard_and_readers_to_docs():
    """The wordmark had `/docs/` hardcoded in both shell partials, so
    clicking the brand inside the ops UI navigated out to the public
    API reference."""
    from config.context_processors import app_meta

    staff = app_meta(_staff_request("/ops/"))["brand_url"]
    assert staff == reverse("brainconfig:dashboard")

    anon_request = RequestFactory().get("/docs/")
    anon_request.user = AnonymousUser()
    assert app_meta(anon_request)["brand_url"] == reverse("docs:index")

    # No auth middleware (error pages, hand-built requests): still resolves.
    assert app_meta(RequestFactory().get("/"))["brand_url"] == reverse("docs:index")


def test_docs_dashboard_pivot_points_at_the_ops_dashboard():
    """The specific regression: not just non-'#', but the right target."""
    ctx = build_layout_context(_staff_request("/docs/"), active="index")
    rows = dict(_urls(ctx["docs_nav_sections"]))
    assert rows["Dashboard"] == reverse("brainconfig:dashboard")

    top = ctx["docs_top_links"]
    assert [link["url"] for link in top] == [reverse("brainconfig:dashboard")]
