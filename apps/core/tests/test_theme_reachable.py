"""Dark mode is reachable: something reads the stored choice, and
something lets the visitor make one.

Every piece of the theme existed except the two that connect it. The
`.dark` block in tokens.css, the compiled `dark:` utilities, the
explorer's repaint observer and `/ops/styleguide/` rendering both themes
were all real work — and no template included
`partials/_theme_toggle.html`, and nothing anywhere called
`localStorage.getItem('theme')`. So `.dark` never landed on <html> and
none of it ran.

Both halves fail silently and independently, which is why they are
asserted separately: a toggle nobody includes looks fine in review, and
a reader with no toggle looks fine too.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = REPO_ROOT / "templates"

BASE = TEMPLATES / "base.html"
TOGGLE = TEMPLATES / "partials/_theme_toggle.html"


_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL)


def _theme_script() -> tuple[str, str]:
    """(attributes, body) of the inline script that resolves the theme.

    Located by what it does, not by position — the surrounding
    `{% comment %}` block names the same symbols in prose, so a plain
    substring search over the file finds the wrong thing.
    """
    for attrs, body in _SCRIPT.findall(BASE.read_text(encoding="utf-8")):
        if "localStorage.getItem('theme')" in body:
            return attrs, body
    raise AssertionError(
        "No inline script in base.html reads the persisted theme, so "
        "`.dark` is never applied on load and the whole dark palette is "
        "dead. That single missing read is the original bug."
    )


def test_something_reads_the_stored_theme() -> None:
    """The missing half. Without it the toggle writes a value no page
    ever reads back, so the choice survives nothing."""
    _attrs, body = _theme_script()
    assert "classList" in body and "'dark'" in body, (
        "the theme script reads localStorage but never applies the class"
    )


def test_the_theme_script_is_not_deferred() -> None:
    """A deferred theme script runs after parsing, so the page paints
    light and then repaints dark — a flash on every single load."""
    attrs, _body = _theme_script()
    assert "defer" not in attrs, f"the theme script is deferred:<script{attrs}>"
    assert "async" not in attrs, f"the theme script is async:<script{attrs}>"


def test_the_theme_script_carries_a_csp_nonce() -> None:
    """Enforced CSP drops an un-nonced inline script and the page still
    renders — the theme would just silently not apply."""
    attrs, _body = _theme_script()
    assert "csp_nonce" in attrs, f"theme script has no nonce:<script{attrs}>"


def test_os_preference_is_honoured_when_nothing_is_stored() -> None:
    """The toggle partial has always claimed it 'respects OS default on
    first visit'. Nothing implemented that."""
    _attrs, body = _theme_script()
    assert "prefers-color-scheme: dark" in body


def test_the_toggle_is_actually_included_somewhere() -> None:
    include = "partials/_theme_toggle.html"
    includers = sorted(
        path.relative_to(TEMPLATES).as_posix()
        for path in TEMPLATES.rglob("*.html")
        if path != TOGGLE and include in path.read_text(encoding="utf-8")
    )
    assert includers, (
        "No template includes the theme toggle, so there is no way to "
        "switch themes anywhere in the UI."
    )


@pytest.mark.parametrize("chrome", ["partials/_topbar.html", "partials/_sidebar.html"])
def test_both_chrome_partials_carry_it(chrome: str) -> None:
    """Ops hides the topbar at `lg` (the sidebar is the chrome there) and
    shows it below `lg`. One include would leave one breakpoint with no
    toggle at all."""
    text = (TEMPLATES / chrome).read_text(encoding="utf-8")
    assert "partials/_theme_toggle.html" in text


def test_the_toggle_uses_semantic_tokens_not_palette_primitives() -> None:
    """CLAUDE.md: dark mode here is a variable swap, so a component that
    names `text-muted` is already right in both themes. The old version
    hardcoded zinc and then patched it with `dark:` counterparts, which a
    re-theme through tokens.css would miss."""
    text = TOGGLE.read_text(encoding="utf-8")
    # Only `class="…"` attributes — the Alpine x-data below declares a
    # boolean literally named `dark`, which a naive scan reads as a
    # `dark:` variant.
    classes = " ".join(re.findall(r'\bclass="([^"]*)"', text))
    assert "dark:" not in classes, f"the toggle still uses `dark:` variants: {classes}"
    primitives = re.findall(
        r"\b(?:bg|text|border|ring)-(?:zinc|slate|gray|indigo)-\d{2,3}\b", classes
    )
    assert not primitives, f"palette primitives in the toggle: {sorted(set(primitives))}"


def test_tokens_css_still_defines_the_dark_palette() -> None:
    """The other direction: this commit must not be readable as 'wire up
    a toggle that swaps to an empty palette'."""
    tokens = (REPO_ROOT / "static/css/tokens.css").read_text(encoding="utf-8")
    assert ".dark {" in tokens
    dark_block = tokens[tokens.index(".dark {") :]
    for required in ("--surface:", "--ink:", "--muted:", "--line:", "--accent:"):
        assert required in dark_block, f"{required} missing from the .dark block"
