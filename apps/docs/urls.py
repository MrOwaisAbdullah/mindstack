"""Docs site URL routes.

Mounted at `/docs/` from `config/urls.py`. Namespaced (`app_name = "docs"`)
because docs URLs are new in this leg — no legacy templates referencing
flat names. Sidebar + topbar use `docs:index` / `docs:endpoint-detail`
/ `docs:guide`.

URL shape:
  /docs/                          → index (catalog grouped by tag)
  /docs/<slug>/                   → per-endpoint detail
  /docs/guide/<slug>/             → one of the 5 static guides

The guide route uses a `guide/<slug>/` prefix instead of `<slug>/`
to avoid name collisions with endpoint slugs (a hypothetical endpoint
named "auth" would shadow the auth guide).
"""
from __future__ import annotations

from django.urls import path

from apps.docs.views import endpoint_detail_view, guide_view, index_view

app_name = "docs"

urlpatterns = [
    path("", index_view, name="index"),
    path("guide/<slug:slug>/", guide_view, name="guide"),
    path("<slug:slug>/", endpoint_detail_view, name="endpoint-detail"),
]
