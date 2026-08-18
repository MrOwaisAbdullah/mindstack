/* ============================================================
   M3.5.4 — live activity feed.

   Polls /ops/activity.json with a cursor and re-broadcasts each read
   event as a DOM event, so any visual on the page can react without
   knowing anything about the transport:

       document.addEventListener("brain:reads", function (e) {
         e.detail.events.forEach(...)   // {id, at, entity_ids, endpoint, …}
       });

   Polling beats SSE here: an ops page left open all day shouldn't hold a
   streaming connection, and a missed tick costs nothing — the cursor
   collects everything on the next one. Polling stops while the tab is
   hidden and catches up when it comes back.
   ============================================================ */
(function (global) {
  "use strict";

  var INTERVAL_MS = 4000;

  function start(url) {
    var cursor = 0;
    var timer = null;
    var stopped = false;

    function tick() {
      if (stopped || document.visibilityState === "hidden") return schedule();
      var target = url + (cursor ? "?after=" + cursor : "");
      fetch(target, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) throw new Error(r.status);
          return r.json();
        })
        .then(function (data) {
          cursor = data.last_id || cursor;
          if (data.events && data.events.length) {
            document.dispatchEvent(
              new CustomEvent("brain:reads", { detail: { events: data.events } })
            );
          }
        })
        .catch(function () {
          /* A failed poll is not an error worth shouting about: the next
             tick retries from the same cursor. */
        })
        .then(schedule);
    }

    function schedule() {
      clearTimeout(timer);
      if (!stopped) timer = setTimeout(tick, INTERVAL_MS);
    }

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") {
        clearTimeout(timer);
        tick();
      }
    });

    tick();
    return {
      stop: function () {
        stopped = true;
        clearTimeout(timer);
      },
      cursor: function () {
        return cursor;
      }
    };
  }

  global.BrainActivity = { start: start };

  document.addEventListener("DOMContentLoaded", function () {
    var host = document.querySelector("[data-activity-url]");
    if (host) global.BrainActivity.live = start(host.getAttribute("data-activity-url"));
  });
})(window);
