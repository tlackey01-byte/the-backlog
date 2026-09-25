// Which backend this copy of the site talks to: the Firebase project (sign-in + saved data) and
// the backlog proxy Worker. Loaded as a plain script in every page's <head>, before the page's
// own scripts, which read window.BacklogEnv.
//
// The live site (GitHub Pages) uses production. Any other address -- localhost, or a Cloudflare
// Pages branch preview such as igdb-search.the-backlog.pages.dev -- uses the dev backend, so
// testing a branch can never touch real data (deletes and syncs there are permanent).
//
// To test locally against production on purpose (the old local setup), open the page with
// ?env=prod once; this browser remembers it for localhost until ?env=dev. Previews can't switch:
// anyone with a preview's link can open it, so it never gets production.
(function () {
  "use strict";

  var PROD_HOSTS = ["tlackey01-byte.github.io"];
  var ENVS = {
    prod: {
      // Not a secret: Firebase's access control is Firestore Security Rules + Authentication.
      firebase: {
        apiKey: "AIzaSyD6xJX12ovVZ7Hi8Xtwu0L3CHmgKB3-BzY",
        authDomain: "the-backlog-34b22.firebaseapp.com",
        projectId: "the-backlog-34b22",
        storageBucket: "the-backlog-34b22.firebasestorage.app",
        messagingSenderId: "306665266277",
        appId: "1:306665266277:web:129933a71722256ba9ba8c",
      },
      proxyUrl: "https://backlog-proxy.tlackey01.workers.dev",
    },
    dev: {
      // The the-backlog-dev Firebase project: its own sign-in and database. If this is ever
      // emptied (null), dev pages say so instead of offering a sign-in.
      firebase: {
        apiKey: "AIzaSyD8Gzf6GDPK3dArfxnbqIkRPENOo6l1jAo",
        authDomain: "the-backlog-dev.firebaseapp.com",
        projectId: "the-backlog-dev",
        storageBucket: "the-backlog-dev.firebasestorage.app",
        messagingSenderId: "171173972971",
        appId: "1:171173972971:web:556048e0f040c74c7d67ae",
      },
      // `npx wrangler deploy --env dev` (see worker/wrangler.toml).
      proxyUrl: "https://backlog-proxy-dev.tlackey01.workers.dev",
    },
  };

  var host = location.hostname;
  var local = host === "localhost" || host === "127.0.0.1";
  var name = PROD_HOSTS.indexOf(host) > -1 ? "prod" : "dev";
  if (local) {
    try {
      var asked = new URLSearchParams(location.search).get("env");
      if (asked === "prod" || asked === "dev") localStorage.setItem("backlog-env", asked);
      if (localStorage.getItem("backlog-env") === "prod") name = "prod";
    } catch (e) {}
  }

  window.BacklogEnv = { name: name, local: local, firebase: ENVS[name].firebase, proxyUrl: ENVS[name].proxyUrl };

  // Anywhere but the live site, a corner badge says whose data this is: a local page on
  // production data looks exactly like a dev one otherwise.
  if (name === "prod" && !local) return;
  function badge() {
    var el = document.createElement("div");
    el.textContent = name === "prod" ? "Local · production data" : "Dev";
    el.setAttribute("aria-hidden", "true");
    el.style.cssText = "position:fixed;right:10px;bottom:calc(10px + env(safe-area-inset-bottom));z-index:1000;" +
      "pointer-events:none;padding:4px 9px;border-radius:999px;font:600 11px/1.3 var(--font-mono, monospace);" +
      "letter-spacing:.06em;text-transform:uppercase;color:#fff;opacity:.92;background:" +
      (name === "prod" ? "#a1481f" : "#6a5fc4");
    document.body.appendChild(el);
  }
  if (document.body) badge();
  else document.addEventListener("DOMContentLoaded", badge);
})();
