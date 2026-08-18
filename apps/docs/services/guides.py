"""Static guides loader.

Renders Markdown files from `apps/docs/guides/<slug>.md` into HTML
via the `markdown` library. Extensions enabled:

  - `extra`        — tables, fenced code, footnotes, def_lists, attr_list
  - `sane_lists`   — sensible ordered/unordered list parsing
  - `toc`          — slugified heading anchors so deep links work
  - `codehilite`   — Pygments-class spans on fenced code blocks; the
                     matching light + dark stylesheets ship in
                     `static/css/app.css` (regenerate via
                     `scripts/generate_pygments_css.py`).

The slug whitelist lives in `apps.docs.context.GUIDES` (single source
of truth for sidebar + valid URLs); this module just reads from disk
based on the slug the view passes through.

Placeholder substitution: `{{ NAME }}` tokens in the markdown source
are resolved against a small mapping of public-facing settings
(PUBLIC_BASE_URL / SUPPORT_EMAIL / STATUS_URL / PUBLIC_REPO_URL) BEFORE
the markdown is parsed. This keeps the guide content authoring-friendly
(plain markdown, no Jinja) while letting operators override hostnames
without forking the guide files. Unknown tokens pass through unchanged
so a stray `{{ TODO }}` in a draft is visually obvious in the rendered
page.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import markdown as md

log = logging.getLogger(__name__)

GUIDES_DIR = Path(__file__).resolve().parent.parent / "guides"

_EXTENSIONS = ["extra", "sane_lists", "toc", "codehilite"]
_EXTENSION_CONFIGS = {
    "codehilite": {
        "css_class": "highlight",
        "guess_lang": False,
        "linenums": False,
    },
}


@dataclass
class GuideBundle:
    slug: str
    title: str
    html: str


# Strip the leading `# Title` from rendered markdown — the layout
# already renders the page title as <h1>, and a second <h1> in the
# body fails the WCAG "one h1 per page" baseline.
_LEADING_H1_RE = re.compile(r"^\s*<h1[^>]*>.*?</h1>\s*", re.DOTALL)

# Fence header in the markdown source — captures the language hint
# from ```python / ```bash / ``` (no lang). Codehilite uses it to
# pick a Pygments lexer but discards it from the HTML, so we walk
# the source to recover the label for the toolbar.
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})\s*([\w+-]*)\s*$", re.MULTILINE)
_HIGHLIGHT_OPEN = '<div class="highlight">'

# Public-facing placeholder tokens. The set is closed (not arbitrary
# attribute access on settings) so a typo in the markdown can't reach
# a sensitive field.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z_]+)\s*\}\}")
_PUBLIC_SETTING_KEYS = (
    "APP_NAME",
    "PUBLIC_BASE_URL",
    "SUPPORT_EMAIL",
    "STATUS_URL",
    "PUBLIC_REPO_URL",
)


def _resolve_placeholders(text: str) -> str:
    """Substitute `{{ NAME }}` tokens with their values from settings.
    Empty-string settings are substituted as empty (so a guide written
    against an optional setting renders cleanly when it's unconfigured;
    the markdown should be authored to read OK in that case). Unknown
    tokens pass through verbatim — a stray `{{ TODO }}` stays visible."""
    from apps.brainconfig.services import app_name
    from config.settings.env import settings

    mapping = {key: getattr(settings, key, "") for key in _PUBLIC_SETTING_KEYS}
    # APP_NAME is operator-editable on /ops/settings/; the env object only
    # knows the boot-time value, so a renamed install would keep quoting
    # the old name at readers.
    mapping["APP_NAME"] = app_name()
    # The ops console lives under a configurable (and, in prod, deliberately
    # unguessable) prefix, so a guide cannot hardcode `/ops/keys/`. Computed
    # rather than added to _PUBLIC_SETTING_KEYS because the useful value is a
    # whole URL, not the raw setting.
    _ops = "/" + (getattr(settings, "ADMIN_PANEL_URL_PATH", "") or "ops/").strip("/") + "/"
    mapping["OPS_KEYS_URL"] = f"{_ops}consumers/"
    mapping["OPS_LOGS_URL"] = f"{_ops}logs/"
    mapping["OPS_CONNECTORS_URL"] = f"{_ops}connectors/"
    mapping["OPS_SETTINGS_URL"] = f"{_ops}settings/"
    mapping["OPS_HEALTH_URL"] = f"{_ops}health/"

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in mapping:
            return mapping[key]
        return m.group(0)

    return _PLACEHOLDER_RE.sub(repl, text)


def _extract_fence_langs(source: str) -> list[str]:
    """Return the language hint of each top-level fenced block in
    document order. Empty string for unspecified-language fences."""
    langs: list[str] = []
    open_marker: str | None = None
    for match in _FENCE_RE.finditer(source):
        marker, lang = match.group(1), match.group(2)
        if open_marker is None:
            open_marker = marker[0] * len(marker)
            langs.append(lang.lower())
        elif marker.startswith(open_marker[0]) and len(marker) >= len(open_marker):
            open_marker = None
    return langs


def _annotate_languages(html: str, langs: list[str]) -> str:
    """Inject `data-lang=...` on each `<div class="highlight">` in the
    rendered HTML, matched to source fences in order."""
    if not langs:
        return html
    out: list[str] = []
    cursor = 0
    idx = 0
    while True:
        pos = html.find(_HIGHLIGHT_OPEN, cursor)
        if pos == -1:
            out.append(html[cursor:])
            break
        out.append(html[cursor:pos])
        lang = langs[idx] if idx < len(langs) else ""
        idx += 1
        if lang:
            out.append(f'<div class="highlight" data-lang="{lang}">')
        else:
            out.append(_HIGHLIGHT_OPEN)
        cursor = pos + len(_HIGHLIGHT_OPEN)
    return "".join(out)


def render_guide(slug: str, *, title: str) -> GuideBundle | None:
    """Read `<slug>.md` from the guides dir + render to HTML. Returns
    None when the file doesn't exist (the view 404s on that)."""
    path = GUIDES_DIR / f"{slug}.md"
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("docs: guide %s read failed", slug, exc_info=True)
        return None

    # Substitute public-facing placeholders BEFORE markdown parsing —
    # PUBLIC_BASE_URL etc. live in env (see config/settings/env.py) so
    # the rendered hostnames reflect this deployment, not a starter-
    # template default.
    raw = _resolve_placeholders(raw)

    # Fresh parser per call — markdown.Markdown holds state across
    # convert() calls (e.g. the toc extension) so we don't reuse it.
    parser = md.Markdown(
        extensions=list(_EXTENSIONS),
        extension_configs=_EXTENSION_CONFIGS,
        output_format="html",
    )
    html = _LEADING_H1_RE.sub("", parser.convert(raw), count=1)
    html = _annotate_languages(html, _extract_fence_langs(raw))
    return GuideBundle(slug=slug, title=title, html=html)


__all__ = ["GUIDES_DIR", "GuideBundle", "render_guide"]
