"""Template rules that a browser will not tell you about (M5.6.2).

Both checks here exist because the failure was actually made, more than
once, and neither one raises an error anywhere — the page just renders
wrong and you have to be looking at the right pixel to notice.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "templates"

# `style="…"` but NOT `:style="…"` / `x-bind:style="…"`, which are Alpine
# reactive bindings — JS expressions, not inline styles.
INLINE_STYLE = re.compile(r'(?<![:\w-])style="')

# A whole HTML tag, tolerating `>` inside quoted attribute values (Django
# tags and Alpine expressions both contain them).
TAG = re.compile(r"""<[a-zA-Z][a-zA-Z0-9-]*(?:"[^"]*"|'[^']*'|[^>"'])*>""")
CLASS_ATTR = re.compile(r'(?<![:\w-])class\s*=')


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_duplicate_class_attributes():
    """One tag, one `class` attribute.

    Given two, the browser silently applies the FIRST and discards the
    second — no error, no console warning, the styling just doesn't
    happen. Four of these were introduced while converting inline styles
    to classes on elements that already had a class attribute, and the
    only reason they were caught is that someone went looking.
    """
    offenders = []
    for path in _templates():
        src = path.read_text(encoding="utf-8")
        for match in TAG.finditer(src):
            if len(CLASS_ATTR.findall(match.group(0))) > 1:
                line = src[: match.start()].count("\n") + 1
                offenders.append(
                    f"{path.relative_to(REPO)}:{line}  "
                    f"{' '.join(match.group(0).split())[:110]}"
                )
    assert not offenders, (
        "Tags with two class attributes — the browser keeps only the first, "
        "so the second's classes never apply. Merge them:\n  "
        + "\n  ".join(offenders)
    )


def test_no_multiline_hash_comments():
    """Django's `{# … #}` is SINGLE-LINE only.

    Written across several lines it is not a comment at all: every line
    renders verbatim into the page. This shipped on the dashboard, where
    four lines of engineering commentary printed above the fold on the main
    ops page. It then happened twice more while fixing it — and the second
    time was worse, because the prose contained the word `<style>` in angle
    brackets, so the HTML parser opened a real style element, swallowed the
    rest of the page's markup into it, and the un-nonced result tripped the
    CSP. Nothing errors; you just have to see it.
    """
    offenders = []
    for path in _templates():
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\{#", src):
            end = src.find("#}", match.end())
            if end == -1:
                continue
            if "\n" in src[match.start():end]:
                line = src[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(REPO)}:{line}")
    assert not offenders, (
        "Multi-line `{# … #}` comments render as visible page text. Use a "
        "`{% comment %}` block instead:\n  " + "\n  ".join(offenders)
    )


def test_inline_scripts_carry_a_nonce():
    """Every executable inline `<script>` needs `nonce="{{ csp_nonce }}"`.

    The enforced CSP is `script-src 'self' 'nonce-…' 'unsafe-eval'`, so an
    inline script without one is refused outright — and the page still
    renders perfectly, it just does nothing. Three of these were shipping:
    the entire chat send/stream implementation (4.6 KB: submit handler, SSE
    reader, streaming render — the Send button was inert), and the
    auto-refresh on tasks and feed detail, which only render while work is
    in flight, i.e. exactly when they are the point.

    Scripts with a `src` are covered by `'self'` and need no nonce.
    Non-executable data blocks (`type="application/json"`) are not scripts
    as far as CSP is concerned.
    """
    script_open = re.compile(r"<script(?![^>]*\bsrc=)([^>]*)>", re.I)
    offenders = []
    for path in _templates():
        src = path.read_text(encoding="utf-8")
        for match in script_open.finditer(src):
            attrs = match.group(1)
            if "nonce" in attrs:
                continue
            if re.search(r"""type\s*=\s*["'](?!text/javascript|module)""", attrs):
                continue  # data block, never executed
            line = src[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(REPO)}:{line}")
    assert not offenders, (
        "Inline <script> without a nonce — the enforced CSP refuses these "
        'and the feature silently does nothing. Add nonce="{{ csp_nonce }}":'
        "\n  " + "\n  ".join(offenders)
    )


def test_no_inline_style_attributes():
    """The enforced CSP (`style-src 'self' 'nonce-…'`) drops every inline
    style attribute, and a nonce cannot authorise them — per CSP3 a nonce
    makes `'unsafe-inline'` inert, so the two cannot be combined.

    455 of these had accumulated because the Tailwind build had silently
    stopped running and reaching a design token from a template was
    otherwise impossible. Both halves of that are fixed; this keeps them
    fixed. Page-specific dynamic CSS goes in a nonce'd `<style>` BLOCK,
    which the same policy permits.
    """
    offenders = []
    for path in _templates():
        src = path.read_text(encoding="utf-8")
        for match in INLINE_STYLE.finditer(src):
            line = src[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(REPO)}:{line}")
    assert not offenders, (
        f"{len(offenders)} inline style attribute(s); the enforced CSP drops "
        "every one of them. Use token utilities (bg-surface, border-line, "
        "text-muted…), a component class, or a nonce'd <style> block:\n  "
        + "\n  ".join(offenders[:40])
        + ("\n  …" if len(offenders) > 40 else "")
    )
