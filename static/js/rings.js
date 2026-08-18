/* ============================================================
   M3.5.2 — visibility rings.

   The brain drawn as three concentric tiers: `private` core,
   `agents-only` middle, `public` rim. One dot per entity, coloured by
   kind, sized by how often it was actually served (read events), hollow
   when superseded. Tiers are the thing being drawn because tiers are
   what the server actually enforces — per-tier snapshots, PLAN.md §5.

   Data comes from /ops/graph.json (M3.5.1); topic hub nodes are ignored
   here, they belong to the explorer (3.5.3).

   No charting library: a polar layout of ~50 circles is deterministic
   geometry, and the no-CDN rule makes every dependency a vendoring
   decision. Cytoscape gets vendored at 3.5.3 where a force layout
   genuinely needs it.
   ============================================================ */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var SIZE = 760;
  var C = SIZE / 2;

  // Inner → outer. A tier missing from the payload still gets its ring
  // drawn (empty), so the shape of the brain stays legible.
  var TIERS = ["private", "agents-only", "public"];
  var ZONE = { "private": 132, "agents-only": 244, "public": 352 }; // zone outer radius
  var DOT_RING = { "private": 82, "agents-only": 192, "public": 302 }; // where dots sit

  // Kind colours and the read-count size curve are shared with the
  // explorer (brain-viz.js) — the same note must look the same in both.
  var VIZ = window.BrainViz;

  var LABEL_GAP = 0.42; // radians left free at the bottom of each ring for its label
  var MIN_ARC = 26;     // arc length one dot wants before a second lane opens
  var LANE_GAP = 24;    // radial distance between lanes

  function svg(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    if (attrs) {
      for (var k in attrs) {
        if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
      }
    }
    return node;
  }

  /* Colour rides on a CSS class (.viz-kind-*, compiled app.css, from
     the --viz-* tokens), NOT on fill attributes: the theme toggle then
     recolours an already-drawn SVG for free, and the palette stays a
     one-file edit in tokens.css. Unknown kinds get no class and fall
     back to the catalog slot via the CSS var() default. */
  function kindClass(kind) {
    return VIZ.KIND_ORDER.indexOf(kind) === -1 ? "" : " viz-kind-" + kind;
  }

  function dotRadius(reads) {
    return VIZ.dotRadius(reads);
  }

  function kindRank(kind) {
    return VIZ.kindRank(kind);
  }

  /* Dots are laid out clockwise from just past the bottom, so the
     reserved gap sits under the ring label. Sorting by kind turns the
     ring into arcs of colour instead of confetti. */
  function place(nodes, ringRadius) {
    var n = nodes.length;
    if (!n) return [];
    var circumference = 2 * Math.PI * ringRadius;
    var lanes = Math.max(1, Math.ceil((n * MIN_ARC) / circumference));
    var span = 2 * Math.PI - LABEL_GAP;
    var start = Math.PI / 2 + LABEL_GAP / 2;

    return nodes.map(function (node, i) {
      var angle = start + ((i + 0.5) / n) * span;
      var lane = i % lanes;
      var r = ringRadius + (lane - (lanes - 1) / 2) * LANE_GAP;
      return {
        node: node,
        x: C + r * Math.cos(angle),
        y: C + r * Math.sin(angle)
      };
    });
  }

  function tierOf(node) {
    return TIERS.indexOf(node.tier) === -1 ? "private" : node.tier;
  }

  function render(root, data) {
    var entities = data.nodes.filter(function (n) {
      return n.type === "entity";
    });

    root.textContent = "";
    if (!entities.length) {
      var empty = document.createElement("p");
      empty.className = "rings-status";
      empty.textContent = "No entities indexed yet.";
      root.appendChild(empty);
      return;
    }

    var figure = svg("svg", {
      "class": "rings-figure",
      viewBox: "0 0 " + SIZE + " " + SIZE,
      role: "img",
      "aria-label":
        "Visibility rings: " + entities.length + " brain entities by tier"
    });

    // Zones, outermost first: the fills stack, so the private core reads
    // as the densest part of the brain without any extra styling.
    TIERS.slice().reverse().forEach(function (tier) {
      figure.appendChild(
        svg("circle", {
          "class": "rings-band rings-band-" + tier,
          cx: C,
          cy: C,
          r: ZONE[tier]
        })
      );
    });

    var tip = document.createElement("div");
    tip.className = "rings-tip";
    tip.setAttribute("role", "status");

    var focused = 0;

    TIERS.forEach(function (tier) {
      var ring = DOT_RING[tier];
      figure.appendChild(svg("circle", { "class": "rings-guide", cx: C, cy: C, r: ring }));

      var members = entities
        .filter(function (n) {
          return tierOf(n) === tier;
        })
        .sort(function (a, b) {
          return kindRank(a.kind) - kindRank(b.kind) || (a.id < b.id ? -1 : 1);
        });

      var label = svg("text", { "class": "rings-label", x: C, y: C + ring });
      label.appendChild(document.createTextNode(tier + " · "));
      var count = svg("tspan", { "class": "rings-label-count" });
      count.textContent = String(members.length);
      label.appendChild(count);
      figure.appendChild(label);

      place(members, ring).forEach(function (spot) {
        var node = spot.node;
        var dot = svg("circle", {
          // Superseded notes stay in the brain but are history, never a
          // current position (CLAUDE.md §6.6) — drawn hollow.
          "class": "rings-dot" + kindClass(node.kind) +
                   (node.current ? "" : " rings-dot-hollow"),
          "data-kind": node.kind,
          cx: spot.x.toFixed(2),
          cy: spot.y.toFixed(2),
          r: dotRadius(node.reads).toFixed(2),
          "stroke-width": node.current ? 0 : 1.4,
          opacity: node.current ? 0.9 : 0.75
        });

        var title = svg("title");
        title.textContent = node.id + " (" + node.kind + ", " + node.tier + ")";
        dot.appendChild(title);

        dot.addEventListener("mouseenter", function (event) {
          focused += 1;
          figure.classList.add("is-focused");
          dot.style.opacity = "1";
          showTip(tip, root, node, event);
        });
        dot.addEventListener("mousemove", function (event) {
          moveTip(tip, root, event);
        });
        dot.addEventListener("mouseleave", function () {
          focused = Math.max(0, focused - 1);
          if (!focused) figure.classList.remove("is-focused");
          dot.style.opacity = "";
          tip.classList.remove("is-on");
        });
        dot.addEventListener("click", function () {
          if (node.url) window.location.href = node.url;
        });

        figure.appendChild(dot);
      });
    });

    root.appendChild(figure);
    root.appendChild(tip);
    root.appendChild(legend(entities, data));
    watchActivity(root, figure);
  }

  /* M3.5.4 — a served note pings its dot. The rings then show not just
     what the brain holds but what it is actually being asked for. */
  function watchActivity(root, figure) {
    root.pulses = [];
    document.addEventListener("brain:reads", function (evt) {
      evt.detail.events.forEach(function (read) {
        (read.entity_ids || []).forEach(function (id) {
          var dot = findDot(figure, id);
          if (!dot) return; // "INDEX", a raw/ path — nothing to light up
          root.pulses.push(id);
          ping(figure, dot);
        });
      });
    });
  }

  function findDot(figure, id) {
    var dots = figure.querySelectorAll("circle.rings-dot");
    for (var i = 0; i < dots.length; i++) {
      var t = dots[i].querySelector("title");
      if (t && t.textContent.indexOf(id + " (") === 0) return dots[i];
    }
    return null;
  }

  function ping(figure, dot) {
    var halo = svg("circle", {
      "class": "rings-ping" + kindClass(dot.getAttribute("data-kind")),
      cx: dot.getAttribute("cx"),
      cy: dot.getAttribute("cy"),
      r: dot.getAttribute("r")
    });
    figure.appendChild(halo);
    setTimeout(function () {
      if (halo.parentNode) halo.parentNode.removeChild(halo);
    }, 1200);
  }

  function showTip(tip, root, node, event) {
    tip.textContent = "";

    var title = document.createElement("div");
    title.className = "rings-tip-title";
    title.textContent = node.title || node.id;
    tip.appendChild(title);

    var id = document.createElement("div");
    id.className = "rings-tip-id";
    id.textContent = node.id;
    tip.appendChild(id);

    var bits = [node.kind, node.tier, node.reads + (node.reads === 1 ? " read" : " reads")];
    if (!node.current) bits.push("superseded");
    if (node.stale) bits.push("stale");
    var meta = document.createElement("div");
    meta.className = "rings-tip-meta";
    meta.textContent = bits.join(" · ");
    tip.appendChild(meta);

    if (node.topics && node.topics.length) {
      var topics = document.createElement("div");
      topics.className = "rings-tip-meta";
      topics.textContent = node.topics.join(", ");
      tip.appendChild(topics);
    }

    tip.classList.add("is-on");
    moveTip(tip, root, event);
  }

  function moveTip(tip, root, event) {
    var box = root.getBoundingClientRect();
    var x = event.clientX - box.left + 14;
    var y = event.clientY - box.top + 14;
    // Keep the card inside the container rather than widening the page.
    var maxX = box.width - tip.offsetWidth - 4;
    if (x > maxX) x = Math.max(4, event.clientX - box.left - tip.offsetWidth - 14);
    tip.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px)";
  }

  function legend(entities, data) {
    var counts = {};
    entities.forEach(function (n) {
      counts[n.kind] = (counts[n.kind] || 0) + 1;
    });

    var wrap = document.createElement("div");
    wrap.className = "rings-side";

    var row = document.createElement("div");
    row.className = "rings-legend";
    Object.keys(counts)
      .sort(function (a, b) {
        return kindRank(a) - kindRank(b);
      })
      .forEach(function (kind) {
        var item = document.createElement("span");
        item.className = "rings-legend-item";
        var swatch = document.createElement("span");
        swatch.className = "rings-swatch" + kindClass(kind);
        item.appendChild(swatch);
        var label = document.createElement("span");
        label.className = "rings-legend-label";
        label.textContent = kind;
        item.appendChild(label);
        var n = document.createElement("span");
        n.className = "rings-legend-n";
        n.textContent = counts[kind];
        item.appendChild(n);
        row.appendChild(item);
      });
    wrap.appendChild(row);

    var note = document.createElement("p");
    note.className = "rings-note";
    var window_ = data.window_days ? "last " + data.window_days + " days" : "all time";
    note.textContent =
      "Dot size = reads (" + window_ + "); hollow = superseded. " +
      entities.length + " entities · " + data.counts.topics + " topics · " +
      data.counts.reads_counted + " reads counted.";
    wrap.appendChild(note);

    return wrap;
  }

  function mount(root) {
    var url = root.getAttribute("data-graph-url");
    if (!url) return;
    var days = root.getAttribute("data-days");
    if (days) url += (url.indexOf("?") === -1 ? "?" : "&") + "days=" + encodeURIComponent(days);

    var status = document.createElement("p");
    status.className = "rings-status";
    status.textContent = "Reading the index…";
    root.appendChild(status);

    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("graph.json returned " + response.status);
        return response.json();
      })
      .then(function (data) {
        render(root, data);
      })
      .catch(function (err) {
        root.textContent = "";
        var failed = document.createElement("p");
        failed.className = "rings-status";
        failed.textContent = "Could not draw the rings: " + err.message;
        root.appendChild(failed);
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-rings]"), mount);
  });
})();
