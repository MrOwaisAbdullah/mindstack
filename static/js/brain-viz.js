/* ============================================================
   Shared vocabulary for the brain visuals (M3.5).

   The rings, the explorer and everything after them draw the same
   entities, so they must agree on what a `take` looks like and how big
   a well-read note is. That agreement lives here, once.

   Load before rings.js / explorer.js (both are `defer`, so document
   order is execution order).
   ============================================================ */
(function (global) {
  "use strict";

  // The LIGHT-theme categorical set, kept in lockstep with the
  // --viz-* tokens in tokens.css (which also carry a dark-theme set).
  // The rings colour via CSS classes and follow the theme live; these
  // hexes are for consumers that paint colours from JS (explorer,
  // timeline) — they get the dark set when their own polish pass moves
  // them onto the tokens. The old map failed the categorical colour
  // gates: identity was the ink primitive (1.02:1 on the dark surface,
  // i.e. invisible) and take/story were indistinguishable at a glance.
  var KIND_COLOR = {
    identity: "#a63263",
    project: "#c9673a",
    take: "#6366f1",
    story: "#8a2bad",
    lesson: "#1f9e7e",
    fact: "#3878cc",
    lens: "#a87f0e",
    catalog: "#6d7fd0"
  };

  // Display order: what the brain is (identity, projects) before what it
  // knows (notes), reference material last. Also the ring sort order, so
  // each tier reads as arcs of kind instead of confetti.
  var KIND_ORDER = ["identity", "project", "take", "story", "lesson", "fact", "lens", "catalog"];

  // Same slot the CSS var() fallback uses for unknown kinds.
  var FALLBACK_COLOR = "#6d7fd0";
  var DOT_MIN = 3.6;
  var DOT_MAX = 11;

  global.BrainViz = {
    KIND_COLOR: KIND_COLOR,
    KIND_ORDER: KIND_ORDER,
    TOPIC_COLOR: "#b9b9c6",

    /* Theme-aware: the --viz-* tokens (tokens.css) carry a separately
       validated set per theme, so a canvas painted at draw time gets
       the right palette for the theme it draws under. The hex map is
       the fallback for unknown kinds and token-less contexts. */
    color: function (kind) {
      var fallback = KIND_COLOR[kind] || FALLBACK_COLOR;
      try {
        var token = getComputedStyle(document.documentElement)
          .getPropertyValue("--viz-" + kind)
          .trim();
        return token || fallback;
      } catch (err) {
        return fallback;
      }
    },

    kindRank: function (kind) {
      var i = KIND_ORDER.indexOf(kind);
      return i === -1 ? KIND_ORDER.length : i;
    },

    /* Radius from read count. sqrt, not linear: one note served 40 times
       shouldn't swallow its neighbours, but the difference must stay
       visible at a glance. */
    dotRadius: function (reads) {
      return Math.min(DOT_MAX, DOT_MIN + 2.4 * Math.sqrt(reads || 0));
    }
  };
})(window);
