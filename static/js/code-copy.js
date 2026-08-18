// Decorates every Pygments-highlighted code block inside `.prose`
// containers (docs guides + endpoint detail pages) with a header
// toolbar: language label on the left, copy button on the right.
// Loaded with `defer` so it runs after the DOM exists.
(function () {
  "use strict";

  // Inline SVG (Lucide "copy" + "check") — keeps us off any icon
  // font and avoids a flash before Lucide replaces <i data-lucide>.
  var ICON_COPY =
    '<svg xmlns="http://www.w3.org/2000/svg" class="icon-copy" viewBox="0 0 24 24"' +
    ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"' +
    ' stroke-linejoin="round" aria-hidden="true">' +
    '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>' +
    '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>' +
    "</svg>";
  var ICON_CHECK =
    '<svg xmlns="http://www.w3.org/2000/svg" class="icon-check" viewBox="0 0 24 24"' +
    ' fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"' +
    ' stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M20 6 9 17l-5-5"/>' +
    "</svg>";

  // The guides loader injects `data-lang` on the `.highlight` wrapper
  // after rendering (codehilite itself discards the fence language).
  function detectLanguage(highlight) {
    return (highlight.getAttribute("data-lang") || "").trim();
  }

  function buildToolbar(lang) {
    var bar = document.createElement("div");
    bar.className = "code-toolbar";

    var label = document.createElement("span");
    label.className = "code-lang";
    label.textContent = lang || "";
    bar.appendChild(label);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy-btn";
    btn.setAttribute("aria-label", "Copy code to clipboard");
    btn.setAttribute("title", "Copy code");
    btn.innerHTML = ICON_COPY + ICON_CHECK;
    bar.appendChild(btn);

    return { bar: bar, btn: btn };
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy") ? resolve() : reject();
      } catch (e) {
        reject(e);
      } finally {
        document.body.removeChild(ta);
      }
    });
  }

  function attachToolbar(highlight) {
    if (highlight.querySelector(".code-toolbar")) return;
    var pre = highlight.querySelector("pre");
    if (!pre) return;

    var lang = detectLanguage(highlight);
    var parts = buildToolbar(lang);
    highlight.insertBefore(parts.bar, pre);

    parts.btn.addEventListener("click", function () {
      var text = (pre.textContent || "").replace(/\n$/, "");
      copyText(text).then(
        function () {
          parts.btn.dataset.copied = "true";
          parts.btn.setAttribute("aria-label", "Copied!");
          window.setTimeout(function () {
            delete parts.btn.dataset.copied;
            parts.btn.setAttribute("aria-label", "Copy code to clipboard");
          }, 1500);
        },
        function () {
          parts.btn.setAttribute("aria-label", "Copy failed");
        },
      );
    });
  }

  function init() {
    var nodes = document.querySelectorAll(".prose .highlight");
    for (var i = 0; i < nodes.length; i++) attachToolbar(nodes[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
