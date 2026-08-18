"""Rasterise static/img/brain-mark.svg into the favicon set.

    python scripts/build-icons.py

Writes static/favicon.ico (16/32/48) and static/img/apple-touch-icon.png.
Run it after editing brain-mark.svg; the outputs are committed, like
tw.css, because the Docker image ships artifacts and builds neither.

Requirements are HOST-only and deliberately not in the app's
dependencies: Pillow packs the .ico, and Playwright's Chromium is the
SVG rasteriser (there is no cairosvg/resvg here, and Chromium is already
installed for the UI checks). Neither is needed to run the server.
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SVG = REPO / "static" / "img" / "brain-mark.svg"
ICO = REPO / "static" / "favicon.ico"
APPLE = REPO / "static" / "img" / "apple-touch-icon.png"

# 16/32/48 are what browsers and Windows actually pull out of an .ico.
ICO_SIZES = (16, 32, 48)
APPLE_SIZE = 180
# The mark sits at ~62% inside the touch icon: iOS clips to a squircle
# and composites transparency onto black, so it needs margin and an
# opaque background of its own.
APPLE_INNER = 112
PAPER = "#fffef7"

_WRAP = """<!doctype html><meta charset=utf-8>
<style>html,body{{margin:0;padding:0;background:{bg}}}
.box{{width:{s}px;height:{s}px;display:flex;align-items:center;justify-content:center}}
img{{width:{i}px;height:{i}px;display:block}}</style>
<div class=box><img src="{src}"></div>"""


def _render(page_factory, out: pathlib.Path, size: int, *, bg: str, inner: int):
    tmp = out.with_suffix(".tmp.html")
    tmp.write_text(_WRAP.format(bg=bg, s=size, i=inner, src=SVG.as_uri()),
                   encoding="utf-8")
    try:
        page = page_factory(size)
        page.goto(tmp.as_uri())
        page.wait_for_timeout(200)
        page.screenshot(path=str(out), omit_background=(bg == "transparent"))
        page.context.close()
    finally:
        tmp.unlink(missing_ok=True)
    return out


def main() -> int:
    try:
        from PIL import Image
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - host tooling only
        print(f"missing host tool: {exc}. pip install pillow playwright", file=sys.stderr)
        return 1

    if not SVG.exists():
        print(f"missing {SVG}", file=sys.stderr)
        return 1

    tmpdir = REPO / "static" / "img"
    frames = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def factory(size: int):
            return browser.new_context(
                viewport={"width": size, "height": size}).new_page()

        for size in ICO_SIZES:
            # Rendered NATIVELY at each size, never downscaled from one
            # big raster — at 16px a downscale turns the fissure to mush.
            frames.append(_render(factory, tmpdir / f"_ico-{size}.png", size,
                                  bg="transparent", inner=size))
        _render(factory, APPLE, APPLE_SIZE, bg=PAPER, inner=APPLE_INNER)
        browser.close()

    imgs = [Image.open(f).convert("RGBA") for f in frames]
    # Base must be the LARGEST frame: Pillow's ICO writer silently drops
    # requested sizes larger than the base image.
    imgs[-1].save(ICO, format="ICO",
                  sizes=[(i.width, i.height) for i in imgs],
                  append_images=imgs[:-1])
    for img, f in zip(imgs, frames):
        img.close()
        f.unlink(missing_ok=True)

    print(f"favicon.ico       {ICO.stat().st_size:>6}B  "
          f"{sorted(Image.open(ICO).ico.sizes())}")
    print(f"apple-touch-icon  {APPLE.stat().st_size:>6}B  {Image.open(APPLE).size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
