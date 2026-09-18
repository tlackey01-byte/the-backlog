// Section navigation shared by every page (homepage + each media-type section).
//
// SECTIONS is the one place sections are declared. To launch a new section: build its
// page, then flip `enabled` to true here -- every page's navigation picks it up with no
// layout changes. Phones get a bottom tab bar (one equal-width slot per enabled section,
// so 2 or 5 tabs lay out the same way); wide screens get the same list as inline links in
// the header's #section-nav-slot. Both stay hidden while only one section is enabled,
// since a one-tab bar is just noise.
//
// Usage: <script src="<root>shared/nav.js" data-active="games" data-root="../"></script>
// (data-root is the path from the page back to the site root, "" on the homepage).
(function () {
  "use strict";

  var SECTIONS = [
    { key: "games",  label: "Games",  icon: "game",  href: "games/",  enabled: true },
    { key: "books",  label: "Books",  icon: "book",  href: "books/",  enabled: false },
    { key: "movies", label: "Movies", icon: "film",  href: "movies/", enabled: false },
    { key: "tv",     label: "TV",     icon: "tv",    href: "tv/",     enabled: false },
    { key: "music",  label: "Music",  icon: "music", href: "music/",  enabled: false }
  ];

  var ICON_PATHS = {
    game: '<rect x="2.5" y="7" width="19" height="11" rx="5.5"/><path d="M7.5 11v3M6 12.5h3"/><circle cx="15.5" cy="12" r=".9"/><circle cx="17.8" cy="14" r=".9"/>',
    book: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5"/>',
    film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    tv: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M8 3l4 3 4-3"/>',
    music: '<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'
  };
  function icon(name, size) {
    return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICON_PATHS[name] || '') + '</svg>';
  }

  var script = document.currentScript;
  var active = script ? script.getAttribute("data-active") : "";
  var root = script ? (script.getAttribute("data-root") || "") : "";

  window.BacklogNav = { SECTIONS: SECTIONS, icon: icon };

  function mount() {
    var enabled = SECTIONS.filter(function (s) { return s.enabled; });
    if (enabled.length < 2) return;

    var links = enabled.map(function (s) {
      var on = s.key === active;
      return '<a class="section-link' + (on ? " active" : "") + '" href="' + root + s.href + '"' +
        (on ? ' aria-current="page"' : "") + ">" + icon(s.icon, 22) + "<span>" + s.label + "</span></a>";
    }).join("");

    var slot = document.getElementById("section-nav-slot");
    if (slot) slot.innerHTML = '<nav class="section-links" aria-label="Sections">' + links + "</nav>";

    var bar = document.createElement("nav");
    bar.className = "section-tabbar";
    bar.setAttribute("aria-label", "Sections");
    bar.innerHTML = links;
    document.body.appendChild(bar);
    document.body.classList.add("has-tabbar");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
