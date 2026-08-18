"""Static-files storage subclass (adjacent).

`TolerantManifestStaticFilesStorage` extends WhiteNoise's
`CompressedManifestStaticFilesStorage` with `manifest_strict = False` so
collectstatic doesn't fail when a vendored JS bundle references a
sibling we intentionally don't ship (typical case: `lucide.min.js`
references `lucide.min.js.map` for browser devtools source-mapping —
the map is dev-only and not part of the production bundle).

Strict mode is the right default for hand-authored CSS/JS where a
missing reference IS a real bug. It's the wrong default for vendored
bundles where the references are external concerns the operator can't
fix without forking the vendored file.
"""
from __future__ import annotations

from whitenoise.storage import CompressedManifestStaticFilesStorage


class TolerantManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Like the parent, but `manifest_strict = False` so missing referenced
    files (typically source maps) log a warning instead of raising."""

    manifest_strict = False
