# Vendored front-end libraries

Everything the UI loads is served from this app. **No CDN, anywhere** —
a self-hosted brain that phones out to a third party for its own
dashboard isn't self-hosted. It also keeps the CSP at
`script-src 'self'` and keeps the app working on an air-gapped box.

Refresh a library by re-downloading the same dist file (`npm pack <lib>`,
copy `dist/…`), bumping the version here, and committing the diff.

| File | Version | License | Used by |
|---|---|---|---|
| `cytoscape.min.js` | 3.34.0 | MIT (`cytoscape.LICENSE.txt`) | graph explorer (M3.5.3) — force layout |
| `alpine.min.js` | 3.14.9 | MIT | ops UI interactivity |
| `htmx.min.js` | 2.0.4 | Zero-Clause BSD | partial updates |
| `apexcharts.min.js` | (from the starter template) | MIT | charts |
| `lucide.min.js` | (from the starter template) | ISC | icons |
| `focus-trap.js` | (from the starter template) | MIT | modal focus handling |

The visibility rings (M3.5.2) deliberately use **no** library: a polar
layout of a few dozen circles is plain SVG geometry, and every
dependency here is a dependency someone self-hosting has to trust.
