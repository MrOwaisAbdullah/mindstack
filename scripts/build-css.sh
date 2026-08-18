#!/usr/bin/env bash
# Rebuild static/css/tw.css (the compiled Tailwind build that base.html
# loads) from assets/css/app.css.
#
# Uses the Tailwind v4 STANDALONE CLI — no Node, no npm, no package.json,
# no node_modules anywhere in the repo. The binary is downloaded once into
# .cache/ (gitignored). v4 is configured CSS-first, so there is no JS
# config file to pass: app.css carries the @theme, @source and @plugin
# directives.
#
# COMMIT the rebuilt tw.css: the Docker image ships the committed artifact
# and has no CSS build step, so self-hosters need no toolchain either.
# `apps/core/tests/test_css_build.py` fails if the committed file stops
# matching what this script produces.
#
#   bash scripts/build-css.sh          # one-shot
#   bash scripts/build-css.sh --watch  # rebuild on template save
#
# PowerShell twin: `.\dev.ps1 css [-Watch]`. Both read the pinned version
# from scripts/tailwind-version.txt — keep it that way, or the two can
# produce different output and the build check starts flapping.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(tr -d '[:space:]' < scripts/tailwind-version.txt)"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) ASSET=tailwindcss-windows-x64.exe ;;
  Darwin) ASSET=tailwindcss-macos-$([ "$(uname -m)" = arm64 ] && echo arm64 || echo x64) ;;
  *) ASSET=tailwindcss-linux-$([ "$(uname -m)" = aarch64 ] && echo arm64 || echo x64) ;;
esac

BIN=".cache/tailwindcss-${VERSION}-${ASSET}"
if [ ! -x "$BIN" ]; then
  mkdir -p .cache
  echo "downloading tailwindcss ${VERSION} (${ASSET})..."
  curl -sfL -o "$BIN" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${VERSION}/${ASSET}"
  chmod +x "$BIN"
fi

"$BIN" -i assets/css/app.css -o static/css/tw.css --minify "$@"
echo "built static/css/tw.css ($(wc -c < static/css/tw.css) bytes)"
