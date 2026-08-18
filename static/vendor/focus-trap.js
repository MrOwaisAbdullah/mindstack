/*!
 * focus-trap (in-repo minimal implementation; Phase P4)
 *
 * Mirrors the public `focus-trap` library's API shape
 * (https://github.com/focus-trap/focus-trap) for the subset Alpine.js
 * modals actually use:
 *
 *   const trap = focusTrap.createFocusTrap(element, opts);
 *   trap.activate();    // focus first tabbable; install tab + esc handlers
 *   trap.deactivate();  // restore prior focus; remove handlers
 *
 * Why hand-rolled instead of the npm package: the public lib carries
 * ~2KB of focusable-element logic we can express in ~50 LOC. The
 * existing repo vendors Alpine + HTMX + ApexCharts + Lucide directly
 * (no npm), so this file matches the established pattern.
 *
 * Options accepted (subset of the public API):
 *   - escapeDeactivates: bool (default true) — Esc deactivates + bubbles.
 *   - clickOutsideDeactivates: bool (default false) — backdrop click
 *     deactivates. Alpine modals already wire backdrop clicks via
 *     `x-on:click="open = false"`, so we keep this off by default.
 *   - returnFocusOnDeactivate: bool (default true) — restore focus
 *     to the element that had it before activate() ran.
 *   - onActivate / onDeactivate: () => void callbacks.
 *
 * The trap is *modal-only* — it does not implement focus-trap's
 * "stack" feature (one trap suspends another), because we don't yet
 * have nested modals. If we add them, switch to the npm package.
 */
(function (root) {
  'use strict';

  // The set of selectors that focus-trap's full implementation walks.
  // Mirrors WCAG 2.1's "keyboard-operable element" definition.
  var TABBABLE_SELECTOR = [
    'a[href]:not([disabled])',
    'button:not([disabled])',
    'textarea:not([disabled])',
    'input:not([disabled])',
    'select:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  function tabbablesIn(root) {
    // Filter out elements that are display:none / visibility:hidden /
    // disabled (which `:not([disabled])` doesn't catch on some host
    // elements). Cheap walk — modals usually have <10 focusable nodes.
    var out = [];
    var candidates = root.querySelectorAll(TABBABLE_SELECTOR);
    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement) {
        out.push(el);
      }
    }
    return out;
  }

  function createFocusTrap(element, opts) {
    if (!element) {
      throw new Error('focus-trap: element is required');
    }
    opts = opts || {};
    var escapeDeactivates = opts.escapeDeactivates !== false;
    var returnFocus = opts.returnFocusOnDeactivate !== false;

    var previouslyFocused = null;
    var keyHandler = null;
    var active = false;

    function onKey(ev) {
      if (ev.key === 'Escape' && escapeDeactivates) {
        ev.preventDefault();
        trap.deactivate();
        return;
      }
      if (ev.key !== 'Tab') return;
      var tabbables = tabbablesIn(element);
      if (tabbables.length === 0) {
        // Nothing focusable inside the trap — pin focus on the
        // container itself so it doesn't escape to background DOM.
        ev.preventDefault();
        element.focus();
        return;
      }
      var first = tabbables[0];
      var last = tabbables[tabbables.length - 1];
      if (ev.shiftKey) {
        if (document.activeElement === first || !element.contains(document.activeElement)) {
          ev.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last || !element.contains(document.activeElement)) {
          ev.preventDefault();
          first.focus();
        }
      }
    }

    var trap = {
      activate: function () {
        if (active) return trap;
        active = true;
        previouslyFocused = document.activeElement;
        var tabbables = tabbablesIn(element);
        if (tabbables.length > 0) {
          tabbables[0].focus();
        } else if (element.tabIndex < 0) {
          // Make the container focusable as a last resort.
          element.setAttribute('tabindex', '-1');
          element.focus();
        }
        keyHandler = onKey;
        document.addEventListener('keydown', keyHandler, true);
        if (typeof opts.onActivate === 'function') opts.onActivate();
        return trap;
      },
      deactivate: function () {
        if (!active) return trap;
        active = false;
        if (keyHandler) {
          document.removeEventListener('keydown', keyHandler, true);
          keyHandler = null;
        }
        if (returnFocus && previouslyFocused && typeof previouslyFocused.focus === 'function') {
          try { previouslyFocused.focus(); } catch (_) { /* element may have detached */ }
        }
        previouslyFocused = null;
        if (typeof opts.onDeactivate === 'function') opts.onDeactivate();
        return trap;
      },
      get active() { return active; },
    };
    return trap;
  }

  root.focusTrap = { createFocusTrap: createFocusTrap };
})(typeof window !== 'undefined' ? window : this);
