// Custom pull-to-refresh, mobile-only. Installed as a PWA (the whole point of the site's
// manifest/service-worker setup), iOS Safari gives standalone apps no pull-to-refresh at
// all, so this fills that gap. Gated on `pointer: coarse` rather than screen width --
// that's "this input is a finger," which is the actual thing "only on mobile" means, as
// opposed to "the window happens to be narrow" (a touchscreen laptop is coarse+desktop;
// a narrow desktop browser window is fine+narrow -- width alone confuses the two).
(function () {
  "use strict";
  if (!window.matchMedia || !window.matchMedia("(pointer: coarse)").matches) return;

  var THRESHOLD = 70;
  var MAX_PULL = 120;

  var indicator = document.createElement("div");
  indicator.className = "pull-refresh-indicator";
  indicator.innerHTML =
    '<span class="pull-refresh-spinner"></span>' +
    '<span class="pull-refresh-label">Pull to refresh</span>';
  document.body.insertBefore(indicator, document.body.firstChild);
  var label = indicator.querySelector(".pull-refresh-label");

  var startY = null;
  var pulling = false;

  function atTop() {
    return (window.scrollY || document.documentElement.scrollTop || 0) <= 0;
  }

  function reset() {
    startY = null;
    if (!pulling) return;
    pulling = false;
    indicator.style.transform = "";
    indicator.style.opacity = "";
    indicator.classList.remove("ready");
  }

  // passive: true throughout -- this never calls preventDefault(), so it rides alongside
  // native scrolling/bounce rather than fighting it or costing scroll performance.
  document.addEventListener(
    "touchstart",
    function (e) {
      startY = atTop() ? e.touches[0].clientY : null;
      pulling = false;
    },
    { passive: true }
  );

  document.addEventListener(
    "touchmove",
    function (e) {
      if (startY == null) return;
      // No atTop() re-check here on purpose: reading scrollY/scrollTop is a
      // layout-dependent property, and touchmove can fire dozens of times per gesture --
      // re-checking on every single one forces a synchronous layout that pass on this
      // page's fairly large DOM, and was making every tap near the top of the page (the
      // toolbar/filter buttons, which sit right there) feel laggy, not just actual pulls.
      // It's also unnecessary: during a real overscroll pull, the document's scrollTop
      // stays pinned at 0 throughout (the pull is an elastic bounce, not real scrolling),
      // so the touchstart-time check above already covers the only case that matters.
      var delta = e.touches[0].clientY - startY;
      if (delta <= 0) { reset(); return; }
      pulling = true;
      // Square-root easing: quick to start responding, harder to keep pulling past the
      // threshold, so it doesn't feel like it's about to fire the instant you touch the screen.
      var damped = Math.min(MAX_PULL, Math.sqrt(delta) * 8);
      indicator.style.transform = "translateY(" + damped + "px)";
      indicator.style.opacity = String(Math.min(1, damped / THRESHOLD));
      var ready = damped >= THRESHOLD;
      indicator.classList.toggle("ready", ready);
      label.textContent = ready ? "Release to refresh" : "Pull to refresh";
    },
    { passive: true }
  );

  document.addEventListener(
    "touchend",
    function () {
      if (pulling && indicator.classList.contains("ready")) {
        indicator.classList.add("refreshing");
        label.textContent = "Refreshing…";
        location.reload();
        return;
      }
      reset();
    },
    { passive: true }
  );

  document.addEventListener("touchcancel", reset, { passive: true });
})();
