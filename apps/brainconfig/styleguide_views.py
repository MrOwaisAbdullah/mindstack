"""Living style guide for the ops UI component layer (M5.6.1).

Renders every component in `static/css/app.css`'s component layer in BOTH
themes on one page. It exists to make the design system reviewable without
clicking through thirteen pages, and it is where dark mode actually gets
checked — a component that only ever appears on one page in one theme is a
component nobody looks at until a user does.

Deliberately DEBUG-only *and* staff-only. It ships in the repo because it
is how you review a change to the component layer, but it is not part of
the product surface and must not be reachable on a deployment: `urls.py`
only registers the route when `settings.DEBUG` is on, and the view
re-checks at request time so that flipping DEBUG cannot leave a stale
route behind in a long-running process.

The swatch/component data lives here rather than in the template so the
guide enumerates the vocabulary in one readable place — adding a variant
to app.css and forgetting to show it here is the failure mode worth
designing against.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404
from django.shortcuts import render

from .nav import ops_context

# (css variable, label, what it is for). Rendered as swatches in both
# themes so a token that vanishes against its own surface is obvious.
SEMANTIC_TOKENS = [
    ("--surface", "surface", "page + card background"),
    ("--surface-2", "surface-2", "recessed chrome: sidebar, inputs"),
    ("--ink", "ink", "body text"),
    ("--muted", "muted", "secondary text, labels"),
    ("--line", "line", "every border in the app"),
    ("--accent", "accent", "primary action, focus"),
    ("--accent-strong", "accent-strong", "hover on accent"),
    ("--accent-contrast", "accent-contrast", "text ON accent"),
    ("--signal", "signal", "ok / success"),
    ("--warn", "warn", "warning"),
    ("--danger", "danger", "destructive / error"),
]

TERMINAL_TOKENS = [
    ("--c-ink-900", "term-900", "log pane background"),
    ("--c-ink-700", "term-700", "log pane border"),
    ("--c-ink-300", "term-300", "log pane text"),
]

BUTTONS = [
    ("btn-primary", "Primary"),
    ("btn-ghost", "Ghost"),
    ("btn-danger", "Danger"),
    ("btn-danger-ghost", "Danger ghost"),
]

BADGES = [
    ("badge-ok", "ok"),
    ("badge-warn", "warn"),
    ("badge-danger", "danger"),
    ("badge-info", "info"),
    ("badge-muted", "muted"),
]

DOTS = [
    ("dot-ok", "ok"),
    ("dot-warn", "warn"),
    ("dot-danger", "danger"),
    ("dot-info", "info"),
    ("dot-muted", "muted"),
    ("dot-live", "live (pulsing)"),
]

NOTICES = [
    ("notice", "Plain notice — neutral information."),
    ("notice-ok", "Success notice — the thing worked."),
    ("notice-warn", "Warning notice — works, but you should know."),
    ("notice-danger", "Danger notice — it failed, here is why."),
]


@staff_member_required(login_url="login")
def styleguide(request):
    # Belt and braces: urls.py already gates registration on DEBUG, but a
    # process that somehow kept the route must still refuse to serve it.
    if not settings.DEBUG:
        raise Http404

    context = {
        "semantic_tokens": SEMANTIC_TOKENS,
        "terminal_tokens": TERMINAL_TOKENS,
        "buttons": BUTTONS,
        "badges": BADGES,
        "dots": DOTS,
        "notices": NOTICES,
    }
    context.update(ops_context(request))
    return render(request, "ops/styleguide.html", context)
