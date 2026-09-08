// Custom pull-to-refresh, mobile-only. Installed as a PWA (the whole point of the site's
// manifest/service-worker setup), iOS Safari gives standalone apps no pull-to-refresh at
// all, so this fills that gap. Gated on `pointer: coarse` rather than screen width --
// that's "this input is a finger," which is the actual thing "only on mobile" means, as
// opposed to "the window happens to be narrow" (a touchscreen laptop is coarse+desktop;
// a narrow desktop browser window is fine+narrow -- width alone confuses the two).
(function () {
  "use strict";
  if (!window.matchMedia || !window.matchMedia("(pointer: coarse)").matches) return;

  // TRIGGER_PX is how far you actually have to pull before release triggers a refresh --
  // a fixed distance, deliberately independent of device/indicator size, matching the
  // ~60-70px most native pull-to-refresh implementations converge on (Twitter, Gmail,
  // iOS Mail among them): far enough that an incidental drag near the top of the page
  // can't trigger it by accident, close enough that a deliberate pull doesn't feel like
  // it's being ignored.
  //
  // maxPull is a different concern -- how far the indicator can visually travel to fully
  // reveal itself -- and does need to track the device: it's the indicator's own rendered
  // height, which includes env(safe-area-inset-top) for the notch/Dynamic Island, so it
  // varies by device. Re-measured on every touchstart, which also keeps it correct across
  // an orientation change. Deriving the trigger point from this (as an earlier version of
  // this file did) made the trigger distance itself vary by device too, which wasn't the
  // intent -- it was arbitrarily easier to trigger on a device with a larger inset.
  var TRIGGER_PX = 64;
  var maxPull = 120;

  var indicator = document.createElement("div");
  indicator.className = "pull-refresh-indicator";
  indicator.innerHTML =
    '<span class="pull-refresh-spinner"></span>' +
    '<span class="pull-refresh-label">Pull to refresh</span>';
  document.body.insertBefore(indicator, document.body.firstChild);
  var label = indicator.querySelector(".pull-refresh-label");

  function measure() {
    maxPull = indicator.offsetHeight || maxPull;
  }
  measure();

  var startX = null;
  var startY = null;
  var pulling = false;
  // null = not yet decided, 'vertical' = committed to pull-tracking, 'horizontal' =
  // committed to "this is a sideways swipe, ignore it for the rest of this touch."
  var direction = null;
  var DIRECTION_LOCK_PX = 8;

  function atTop() {
    return (window.scrollY || document.documentElement.scrollTop || 0) <= 0;
  }

  function reset() {
    startX = null;
    startY = null;
    direction = null;
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
      var eligible = atTop();
      startX = eligible ? e.touches[0].clientX : null;
      startY = eligible ? e.touches[0].clientY : null;
      direction = null;
      pulling = false;
      if (eligible) measure();
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
      var t = e.touches[0];
      var dx = t.clientX - startX;
      var dy = t.clientY - startY;

      // Direction lock: the nav bar's tab row sits right at the top of the page (where
      // atTop() is also true) and scrolls horizontally, so without this, swiping it
      // sideways got misread as a downward pull the moment the touch had *any* vertical
      // component -- even a few stray pixels from a not-perfectly-horizontal finger drag
      // -- which was reloading the page mid-swipe. Wait for enough movement to be sure,
      // then commit to one interpretation for the rest of this touch.
      if (direction == null) {
        if (Math.abs(dx) < DIRECTION_LOCK_PX && Math.abs(dy) < DIRECTION_LOCK_PX) return;
        direction = Math.abs(dx) >= Math.abs(dy) ? "horizontal" : "vertical";
        if (direction === "horizontal") { reset(); return; }
      }

      if (dy <= 0) { reset(); return; }
      pulling = true;
      // Square-root easing: quick to start responding, harder to keep pulling past the
      // threshold, so it doesn't feel like it's about to fire the instant you touch the screen.
      var damped = Math.min(maxPull, Math.sqrt(dy) * 8);
      // Clamped to maxPull as a safety net in case some device ever measures an
      // indicator shorter than the trigger distance -- keeps the trigger reachable.
      var threshold = Math.min(TRIGGER_PX, maxPull);
      indicator.style.transform = "translateY(" + damped + "px)";
      indicator.style.opacity = String(Math.min(1, damped / threshold));
      var ready = damped >= threshold;
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
