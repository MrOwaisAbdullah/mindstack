"""Exactly one nav item is active, and it is the right one (M5.6.3.1).

`nav.py` emitted no active flag at all until now, so the sidebar never
showed where you were. The interesting case is that the dashboard lives at
the ops root, which prefixes every other ops URL — a naive `startswith`
highlights Dashboard on all ten pages, which is worse than highlighting
nothing because it looks deliberate.
"""
from __future__ import annotations

import pytest
from django.test import RequestFactory

from apps.brainconfig.nav import ops_context


def _active_labels(path: str) -> list[str]:
    request = RequestFactory().get(path)
    ctx = ops_context(request)
    return [
        item["label"]
        for section in ctx["ops_nav_sections"]
        for item in section["items"]
        if item.get("active")
    ]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/ops/", "Dashboard"),
        ("/ops/brain/", "Brain browser"),
        ("/ops/graph/", "Graph"),
        ("/ops/timeline/", "Timeline"),
        ("/ops/feeds/", "Feed queue"),
        ("/ops/chat/", "Chat"),
        ("/ops/tasks/", "Tasks"),
        ("/ops/logs/", "Logs"),
        ("/ops/consumers/", "API keys"),
        ("/ops/settings/", "Settings"),
        ("/ops/health/", "Setup & health"),
        # Detail pages have no nav entry and must highlight their parent —
        # this is why the match is by prefix and not exact-only.
        ("/ops/brain/take-2026-07-rag-vs-finetuning/", "Brain browser"),
        ("/ops/feeds/42/", "Feed queue"),
        ("/ops/chat/7/", "Chat"),
    ],
)
def test_exactly_one_item_is_active_and_it_is_the_right_one(path, expected):
    labels = _active_labels(path)
    assert labels == [expected], (
        f"{path} highlighted {labels or 'nothing'}, expected ['{expected}']"
    )


def test_dashboard_does_not_win_on_every_page():
    """The regression this is really guarding: `/ops/` prefixes every ops
    URL, so longest-match is what keeps Dashboard from lighting up
    everywhere."""
    for path in ("/ops/logs/", "/ops/settings/", "/ops/brain/", "/ops/health/"):
        assert "Dashboard" not in _active_labels(path)


def test_no_request_means_nothing_active():
    ctx = ops_context(None)
    assert not [
        item
        for section in ctx["ops_nav_sections"]
        for item in section["items"]
        if item.get("active")
    ]
