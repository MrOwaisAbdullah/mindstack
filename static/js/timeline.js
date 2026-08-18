/* ============================================================
   M3.5.5 — belief timeline.

   Two panels over one shared month axis, both drawn from graph.json:

   1. Brain growth — how many notes the brain gained each month,
      stacked by kind.
   2. Belief chains — every `superseded_by` chain as position-over-
      time: the old note, hollow, pointing at what replaced it. The
      brain supersedes, never deletes (CLAUDE.md §5.3), so this is
      the honest record of the operator changing their mind.

   Plain SVG, like the rings: bars and some arrows don't justify a
   charting dependency. And like the rings, every colour comes from
   CSS — kind fills via .viz-kind-* (--kind-color), chrome via the
   semantic tokens — so a mid-session theme toggle repaints both
   figures with no JS. The explorer's MutationObserver pattern is
   only needed where Cytoscape paints a canvas CSS cannot reach;
   inline SVG it can.

   Figures draw at scale 1: the width is measured from the host and
   written as SVG width/viewBox ATTRIBUTES (the CSP drops style=),
   and the figure re-renders when the host resizes. When the host is
   narrower than the slots can legibly compress (SLOT_MIN), the
   figure keeps its width and the host scrolls sideways instead of
   letting 10px labels shrink into 4px smudges on a phone.
   ============================================================ */
(function () {
  "use strict";

  var VIZ = window.BrainViz;
  var SVG_NS = "http://www.w3.org/2000/svg";

  var PAD = { left: 34, right: 18, top: 16, bottom: 26 };
  var SLOT_MAX = 132; // a month never spreads wider than this
  var SLOT_MIN = 34;  // …or compresses narrower than this: scroll instead
  var GROWTH_H = 210;
  var ROW_H = 46;

  function svg(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    for (var k in attrs) {
      if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
    }
    return node;
  }

  function text(x, y, str, cls) {
    var t = svg("text", { x: x, y: y, "class": cls || "tl-axis" });
    t.textContent = str;
    return t;
  }

  // Only known kinds become a class — same guard as the explorer's legend.
  function kindClass(kind) {
    return VIZ.KIND_ORDER.indexOf(kind) === -1 ? "" : " viz-kind-" + kind;
  }

  function emptyState(host, title, body) {
    var box = document.createElement("div");
    box.className = "empty-state";
    var h = document.createElement("p");
    h.className = "empty-state-title";
    h.textContent = title;
    var p = document.createElement("p");
    p.className = "max-w-md text-sm";
    p.textContent = body;
    box.appendChild(h);
    box.appendChild(p);
    host.appendChild(box);
  }

  /* Inclusive month range, gaps filled: a month with no notes is part of
     the story, so it gets a slot rather than being skipped. */
  function monthSpan(months) {
    if (!months.length) return [];
    var sorted = months.slice().sort();
    var out = [];
    var cur = sorted[0];
    var last = sorted[sorted.length - 1];
    var guard = 0;
    while (cur <= last && guard++ < 600) {
      out.push(cur);
      var y = parseInt(cur.slice(0, 4), 10);
      var m = parseInt(cur.slice(5, 7), 10) + 1;
      if (m > 12) {
        m = 1;
        y += 1;
      }
      cur = y + "-" + (m < 10 ? "0" + m : m);
    }
    return out;
  }

  /* The figure's true pixel width: fill the host while slots stay inside
     [SLOT_MIN, SLOT_MAX]. Wider than the host means the host scrolls. */
  function figureWidth(host, months) {
    var w = host.clientWidth || 900;
    var chrome = PAD.left + PAD.right;
    var most = chrome + months * SLOT_MAX;
    var least = chrome + months * SLOT_MIN;
    return Math.max(least, Math.min(w, most));
  }

  function drawGrowth(host, dated, axis, kindList, W) {
    var plotW = W - PAD.left - PAD.right;
    var plotH = GROWTH_H - PAD.top - PAD.bottom;
    var slot = plotW / Math.max(1, axis.length);

    var perMonth = {};
    dated.forEach(function (e) {
      perMonth[e.date] = perMonth[e.date] || {};
      perMonth[e.date][e.kind] = (perMonth[e.date][e.kind] || 0) + 1;
    });

    var peak = 1;
    axis.forEach(function (m) {
      var n = 0;
      kindList.forEach(function (k) {
        n += (perMonth[m] || {})[k] || 0;
      });
      peak = Math.max(peak, n);
    });

    var fig = svg("svg", {
      "class": "tl-figure",
      width: W,
      height: GROWTH_H,
      viewBox: "0 0 " + W + " " + GROWTH_H,
      role: "img",
      "aria-label": "Notes added per month, stacked by kind"
    });

    fig.appendChild(
      svg("line", {
        "class": "tl-baseline",
        x1: PAD.left,
        y1: PAD.top + plotH,
        x2: W - PAD.right,
        y2: PAD.top + plotH
      })
    );
    fig.appendChild(text(PAD.left - 6, PAD.top + 8, String(peak), "tl-axis tl-axis-end"));

    // Thin the month labels when slots get tight instead of colliding.
    var labelStep = Math.max(1, Math.ceil(46 / slot));

    axis.forEach(function (m, i) {
      var x = PAD.left + i * slot;
      var bw = Math.min(46, slot * 0.62);
      var bx = x + (slot - bw) / 2;
      var y = PAD.top + plotH;
      var monthTotal = 0;

      kindList.forEach(function (kind) {
        var n = (perMonth[m] || {})[kind] || 0;
        if (!n) return;
        monthTotal += n;
        var h = (n / peak) * plotH;
        y -= h;
        var bar = svg("rect", {
          "class": "tl-bar" + kindClass(kind),
          x: bx.toFixed(1),
          y: y.toFixed(1),
          width: bw.toFixed(1),
          height: h.toFixed(1)
        });
        var title = svg("title");
        title.textContent = m + " · " + kind + " ×" + n;
        bar.appendChild(title);
        fig.appendChild(bar);
      });

      if (monthTotal && slot >= 26) {
        fig.appendChild(text(bx + bw / 2, y - 5, String(monthTotal), "tl-value"));
      }
      if (i % labelStep === 0) {
        fig.appendChild(text(x + slot / 2, PAD.top + plotH + 16, m.slice(2), "tl-month"));
      }
    });

    host.appendChild(fig);
  }

  /* Follow `superseded_by` to the end: a chain can be longer than two
     notes, and only the last note is a current position. */
  function chainsOf(nodes, edges) {
    var byId = {};
    nodes.forEach(function (n) {
      if (n.type === "entity") byId[n.id] = n;
    });
    var next = {};
    var hasParent = {};
    edges.forEach(function (e) {
      if (e.type !== "superseded") return;
      next[e.source] = e.target;
      hasParent[e.target] = true;
    });

    return Object.keys(next)
      .filter(function (id) {
        return !hasParent[id]; // start only from the oldest link
      })
      .map(function (id) {
        var chain = [];
        var cur = id;
        var guard = 0;
        while (cur && byId[cur] && guard++ < 50) {
          chain.push(byId[cur]);
          cur = next[cur];
        }
        return chain;
      })
      .filter(function (c) {
        return c.length > 1;
      });
  }

  function drawChains(host, chains, axis, W) {
    if (!chains.length || !axis.length) {
      emptyState(
        host,
        "Every position is current",
        "No note has been superseded yet. When a new claim replaces an old " +
          "one, the old note stays — marked superseded — and the chain " +
          "appears here as a line across time."
      );
      return;
    }

    var H = PAD.top + chains.length * ROW_H + PAD.bottom;
    var plotW = W - PAD.left - PAD.right;
    var slot = plotW / Math.max(1, axis.length);
    var maxChars = Math.max(12, Math.floor(plotW / 6.2));

    var fig = svg("svg", {
      "class": "tl-figure",
      width: W,
      height: H,
      viewBox: "0 0 " + W + " " + H,
      role: "img",
      "aria-label": chains.length + " superseded belief chains over time"
    });

    var defs = svg("defs");
    var marker = svg("marker", {
      id: "tl-arrow",
      viewBox: "0 0 8 8",
      refX: 7,
      refY: 4,
      markerWidth: 6,
      markerHeight: 6,
      orient: "auto"
    });
    marker.appendChild(svg("path", { d: "M0,0 L8,4 L0,8 z", "class": "tl-arrow-head" }));
    defs.appendChild(marker);
    fig.appendChild(defs);

    function xOf(date) {
      var i = axis.indexOf(date);
      if (i === -1) i = date && date > axis[axis.length - 1] ? axis.length - 1 : 0;
      return PAD.left + i * slot + slot / 2;
    }

    chains.forEach(function (chain, row) {
      var y = PAD.top + row * ROW_H + ROW_H / 2;
      fig.appendChild(
        svg("line", { "class": "tl-lane", x1: PAD.left, y1: y, x2: W - PAD.right, y2: y })
      );

      chain.forEach(function (node, i) {
        var x = xOf(node.date);
        if (i > 0) {
          var prev = xOf(chain[i - 1].date);
          fig.appendChild(
            svg("line", {
              "class": "tl-link",
              x1: prev + 8,
              y1: y,
              x2: Math.max(prev + 18, x - 8),
              y2: y,
              "marker-end": "url(#tl-arrow)"
            })
          );
        }
        var dot = svg("circle", {
          "class": "tl-dot" + (node.current ? "" : " tl-dot-hollow") + kindClass(node.kind),
          cx: x,
          cy: y,
          r: 6
        });
        var title = svg("title");
        title.textContent =
          node.id + " (" + node.date + (node.current ? ", current" : ", superseded") + ")";
        dot.appendChild(title);
        // A real link, not a click handler: keyboard-reachable, and the
        // status quo for "clicking a note opens it" everywhere else.
        if (node.url) {
          var a = svg("a", {
            href: node.url,
            "class": "tl-dot-link",
            "aria-label": node.id + (node.current ? ", current position" : ", superseded")
          });
          a.appendChild(dot);
          fig.appendChild(a);
        } else {
          fig.appendChild(dot);
        }
      });

      var head = chain[chain.length - 1];
      var label = head.title || head.id;
      if (label.length > maxChars) label = label.slice(0, maxChars - 1) + "…";
      fig.appendChild(text(PAD.left, y - 14, label, "tl-chain-label"));
    });

    var labelStep = Math.max(1, Math.ceil(46 / slot));
    axis.forEach(function (m, i) {
      if (i % labelStep === 0) {
        fig.appendChild(text(PAD.left + i * slot + slot / 2, H - 8, m.slice(2), "tl-month"));
      }
    });

    host.appendChild(fig);
  }

  function buildLegend(host, dated, kindList) {
    var counts = {};
    dated.forEach(function (e) {
      counts[e.kind] = (counts[e.kind] || 0) + 1;
    });
    kindList.forEach(function (kind) {
      var item = document.createElement("span");
      item.className = "tl-key-item";
      var swatch = document.createElement("span");
      // Class-based colour (--kind-color via .viz-kind-*): the swatch
      // follows the theme live, exactly like the bars it explains.
      swatch.className = "tl-swatch" + kindClass(kind);
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(kind + " "));
      var n = document.createElement("span");
      n.className = "tl-key-n";
      n.textContent = counts[kind] || 0;
      item.appendChild(n);
      host.appendChild(item);
    });
  }

  function mount(root) {
    var url = root.getAttribute("data-graph-url");
    var growthHost = root.querySelector("[data-tl-growth]");
    var beliefHost = root.querySelector("[data-tl-beliefs]");
    var growthNote = root.querySelector("[data-tl-growth-note]");
    var growthCap = root.querySelector("[data-tl-growth-cap]");
    var beliefNote = root.querySelector("[data-tl-belief-note]");
    var beliefKey = root.querySelector("[data-tl-belief-key]");
    var legendHost = root.querySelector("[data-tl-legend]");

    growthHost.classList.add("tl-scroll");
    beliefHost.classList.add("tl-scroll");

    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("graph.json returned " + r.status);
        return r.json();
      })
      .then(function (data) {
        var entities = data.nodes.filter(function (n) {
          return n.type === "entity";
        });
        var dated = entities.filter(function (e) {
          return e.date;
        });
        var chains = chainsOf(data.nodes, data.edges);

        var months = dated.map(function (e) {
          return e.date;
        });
        chains.forEach(function (c) {
          c.forEach(function (n) {
            if (n.date) months.push(n.date);
          });
        });
        var axis = monthSpan(months);

        if (!axis.length) {
          growthNote.textContent = "";
          emptyState(
            growthHost,
            "No dated notes yet",
            "Notes gain their month from the date: field in frontmatter — " +
              "this chart fills as dated notes are compiled into the brain."
          );
          drawChains(beliefHost, [], axis);
          return;
        }

        var kinds = {};
        dated.forEach(function (e) {
          kinds[e.kind] = true;
        });
        var kindList = Object.keys(kinds).sort(function (a, b) {
          return VIZ.kindRank(a) - VIZ.kindRank(b);
        });

        var range = axis[0] + " → " + axis[axis.length - 1];
        var undated =
          entities.length - dated.length +
          " entities carry no date (project cards, identity, catalogs, lenses).";
        growthNote.textContent =
          dated.length + (dated.length === 1 ? " dated note · " : " dated notes · ") +
          axis.length + (axis.length === 1 ? " month" : " months");
        if (growthCap) {
          growthCap.textContent = range + " · " + undated;
        } else {
          growthNote.textContent += " (" + range + ") · " + undated;
        }

        var chainNotes = chains.reduce(function (n, c) {
          return n + c.length;
        }, 0);
        beliefNote.textContent = chains.length
          ? chains.length + (chains.length === 1 ? " chain" : " chains") +
            " · " + chainNotes + " notes" +
            (beliefKey ? "" : " · hollow = superseded, arrow points at what replaced it")
          : "";
        if (beliefKey) beliefKey.hidden = !chains.length;

        buildLegend(legendHost, dated, kindList);

        var lastW = 0;
        function renderAll() {
          lastW = growthHost.clientWidth || 0;
          var W = figureWidth(growthHost, axis.length);
          growthHost.textContent = "";
          beliefHost.textContent = "";
          drawGrowth(growthHost, dated, axis, kindList, W);
          drawChains(beliefHost, chains, axis, W);
        }
        renderAll();

        /* The figures are drawn at pixel scale for a measured width, so a
           resize (sidebar drawer, window, devtools) re-renders them for
           the new width — same reason the explorer calls cy.resize(). */
        if (window.ResizeObserver) {
          var pending = false;
          new window.ResizeObserver(function () {
            if (pending) return;
            pending = true;
            window.requestAnimationFrame(function () {
              pending = false;
              var w = growthHost.clientWidth || 0;
              if (Math.abs(w - lastW) > 24) renderAll();
            });
          }).observe(growthHost);
        }
      })
      .catch(function (err) {
        growthNote.textContent = "Could not draw the timeline: " + err.message;
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-timeline]"), mount);
  });
})();
