"""Guardrail: `static/css/tw.css` is a BUILD ARTIFACT, and must stay one.

Context (docs/UI-REWRITE.md §6). The Tailwind build silently stopped
running at some point: `tailwind.config.js` was vendored but nothing read
it, there was no `package.json`, and `tw.css` sat frozen for days while
templates kept adding utility classes that compiled to nothing. Every
class added after the freeze was a no-op, and nothing anywhere said so.

This test closes that hole from both sides:

  * rebuild the CSS from source and fail if the committed artifact differs
    -- so the artifact cannot go stale, and nobody can hand-edit the
    minified output and have it stick;
  * fail if the source ever loses its `@source` scanning roots -- a build
    that scans nothing still "succeeds", it just emits an empty utility
    set, which is exactly the failure mode we are guarding against.

The build is deterministic: three consecutive runs of Tailwind v4.3.3
produce byte-identical output, which is what makes the comparison a
useful signal rather than a flaky one.

Requires the pinned standalone binary. It is ~107 MB and gitignored in
`.cache/`, so when it is absent the rebuild test SKIPS with instructions
rather than downloading it implicitly or failing a contributor's first
`pytest` run. Set `BRAIN_CSS_BUILD_REQUIRED=1` (CI) to turn that skip
into a failure.
"""
from __future__ import annotations

import os
import re
import platform
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
APP_CSS = REPO / "assets" / "css" / "app.css"
TW_CSS = REPO / "static" / "css" / "tw.css"
VERSION_FILE = REPO / "scripts" / "tailwind-version.txt"

BUILD_REQUIRED = os.environ.get("BRAIN_CSS_BUILD_REQUIRED") == "1"


def _asset_name() -> str:
    """The release asset for this host, matching build-css.sh / dev.ps1."""
    system, machine = platform.system(), platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    if system == "Windows":
        return "tailwindcss-windows-arm64.exe" if arm else "tailwindcss-windows-x64.exe"
    if system == "Darwin":
        return "tailwindcss-macos-arm64" if arm else "tailwindcss-macos-x64"
    return "tailwindcss-linux-arm64" if arm else "tailwindcss-linux-x64"


def _binary() -> Path:
    version = VERSION_FILE.read_text().strip()
    return REPO / ".cache" / f"tailwindcss-{version}-{_asset_name()}"


def test_committed_css_matches_a_fresh_build(tmp_path):
    """`tw.css` is what app.css currently compiles to -- not a stale copy."""
    binary = _binary()
    if not binary.exists():
        msg = (
            f"Tailwind standalone binary not cached at {binary.relative_to(REPO)}. "
            "Run `.\\dev.ps1 css` (or `bash scripts/build-css.sh`) once to fetch it."
        )
        if BUILD_REQUIRED:
            pytest.fail(msg)
        pytest.skip(msg)

    fresh = tmp_path / "tw.css"
    proc = subprocess.run(
        [str(binary), "-i", str(APP_CSS), "-o", str(fresh), "--minify"],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode == 0, f"tailwind build failed:\n{proc.stderr}"

    # Compare EOL-normalized. `.gitattributes` pins tw.css to LF so this
    # should be a no-op, but a contributor with a different core.autocrlf
    # (or a zip download) would otherwise see a 1-byte CRLF diff reported
    # as "your build output is stale", which is both wrong and a
    # thoroughly confusing first-run experience.
    built = fresh.read_bytes().replace(b"\r\n", b"\n")
    committed = TW_CSS.read_bytes().replace(b"\r\n", b"\n")
    if built != committed:
        pytest.fail(
            "static/css/tw.css is out of date with static/css/app.css and the "
            "templates.\n"
            f"  committed: {len(committed):,} bytes\n"
            f"  rebuilt:   {len(built):,} bytes\n"
            "Run `.\\dev.ps1 css` and commit the result. (If you edited tw.css "
            "by hand: don't -- it is generated. Edit app.css instead.)"
        )


def test_source_declares_its_scanning_roots():
    """A build that scans nothing still exits 0 -- it just emits no utilities.

    `@import "tailwindcss" source(none)` deliberately disables automatic
    content detection (it would otherwise walk `data/brain-repo`, the whole
    brain, and `.venv/`). That makes the explicit `@source` list the only
    thing standing between us and an empty stylesheet, so assert it covers
    every place a class name can appear.
    """
    css = APP_CSS.read_text(encoding="utf-8")
    assert 'source(none)' in css, (
        "app.css no longer disables Tailwind's automatic source detection. "
        "Without source(none) the scanner walks the repo root, which "
        "includes data/brain-repo and .venv/."
    )
    for root in (
        "../../templates/**/*.html",
        "../../apps/**/templates/**/*.html",
        "../../apps/**/*.py",
        "../../static/js/**/*.js",
    ):
        assert f'@source "{root}"' in css, f"app.css stopped scanning {root}"


def test_build_output_contains_utilities_from_templates():
    """End-to-end sanity on the committed artifact itself.

    These four classes are used in templates and were ALL missing from the
    frozen pre-M5.6 artifact -- they are the fingerprint of a build that
    is not actually running. `max-w-4xl` was present back then and is kept
    here as a control: if it alone survives, the artifact went stale again.
    """
    css = TW_CSS.read_text(encoding="utf-8")
    for cls in ("max-w-3xl", "max-w-2xl", "ps-5", "list-decimal"):
        assert f".{cls}{{" in css, (
            f"`{cls}` is used in a template but missing from tw.css -- the "
            "committed build is stale. Run `.\\dev.ps1 css`."
        )
    assert ".max-w-4xl{" in css


def test_no_tailwind_source_files_under_static():
    """`static/` holds SERVED assets only — never Tailwind build input.

    `collectstatic` under prod settings post-processes every CSS file it
    collects, and WhiteNoise's manifest storage resolves each `@import`
    to a real file on disk. `@import "tailwindcss"` is not a file, so it
    raises MissingFileError on 'css/tailwindcss', collectstatic exits
    non-zero, and the web container crash-loops before it ever serves a
    request.

    app.css lived in `static/css/` for exactly one commit and this is how
    it was found: booting a blank instance on prod settings. Nothing in
    dev catches it, because dev uses plain StaticFilesStorage and never
    post-processes anything.
    """
    # Matched at the START of a line so that prose in a comment ("app.css
    # maps these into an @theme block", which tokens.css legitimately says)
    # is not mistaken for a directive.
    directive = re.compile(
        r"^\s*@(import|theme|apply|plugin|source|custom-variant)\b", re.M
    )
    offenders = []
    for path in (REPO / "static").rglob("*.css"):
        if path.name == "tw.css":
            continue  # the compiled OUTPUT; contains none of these by design
        text = path.read_text(encoding="utf-8")
        hit = sorted({m.group(1) for m in directive.finditer(text)})
        if hit:
            offenders.append(
                f"{path.relative_to(REPO)} -> @{', @'.join(hit)}"
            )
    assert not offenders, (
        "Tailwind source directives found under static/. These files are "
        "collected and post-processed at deploy time, and an unresolvable "
        "@import fails collectstatic outright. Build sources belong in "
        "assets/:\n  " + "\n  ".join(offenders)
    )
    # And the compiled artifact must stay resolvable.
    assert "@import" not in TW_CSS.read_text(encoding="utf-8"), (
        "tw.css contains an @import; collectstatic will try to resolve it "
        "as a file path and fail the deploy."
    )


def test_no_javascript_tailwind_config():
    """v4 is CSS-first. A stray tailwind.config.js is decorative at best and
    actively misleading at worst -- the old one sat unread for weeks while
    people assumed it was the source of truth."""
    assert not (REPO / "tailwind.config.js").exists(), (
        "tailwind.config.js is back. Tailwind v4 configures via @theme in "
        "static/css/app.css; the standalone CLI has no --config flag."
    )
    assert not (REPO / "package.json").exists(), (
        "package.json is back. The build is deliberately Node-free so "
        "self-hosters and the Docker image need no toolchain."
    )
