// Firebase setup shared by every page on this site (homepage, games section, and any
// future media-type sections). Which project it connects to comes from shared/env.js:
// production on the live site, the dev project everywhere else. That config is NOT a
// secret -- Firebase's access control lives entirely in Firestore Security Rules +
// Authentication, not in hiding it, so it's safe to ship in the public bundle. See
// docs/../Source (rules published via the Firebase console) for the actual access boundary.
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.18.0/firebase-app.js";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
} from "https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js";
import {
  getFirestore,
  doc,
  setDoc,
  onSnapshot,
  collection,
  addDoc,
  deleteDoc,
  query,
  orderBy,
} from "https://www.gstatic.com/firebasejs/12.18.0/firebase-firestore.js";

const env = window.BacklogEnv || {};
// No config means the dev project isn't set up yet: requireLogin() says so instead of
// offering a sign-in that can't work.
const app = env.firebase ? initializeApp(env.firebase) : null;
export const auth = app ? getAuth(app) : null;
export const db = app ? getFirestore(app) : null;
export function signOutUser() { return signOut(auth); }
export { doc, setDoc, onSnapshot, collection, addDoc, deleteDoc, query, orderBy };

/**
 * Gates a page behind Firebase email/password sign-in.
 *
 * Renders a minimal login form into `mountEl` whenever there's no signed-in user, and
 * calls `onSignedIn(user)` once a session exists (a fresh sign-in, or a restored one on
 * page load) -- removing the form at that point. Sign-in only, no self-serve account
 * creation: this app has exactly one user, created once via the Firebase console.
 *
 * Callers are responsible for keeping their own main content hidden (e.g. via a CSS
 * class toggled from `onSignedIn`) until this fires -- this helper only owns the login
 * form itself, so every page's markup stays free to differ.
 */
export function requireLogin(mountEl, onSignedIn) {
  if (!app) {
    mountEl.insertAdjacentHTML("beforeend",
      '<div class="login-gate"><div class="login-form">' +
        '<div class="login-eyebrow">The Backlog · ' + (env.name || "dev") + '</div>' +
        '<h1 class="login-title">Not set up yet</h1>' +
        '<p style="margin:0;color:var(--text-muted);line-height:1.5">This copy of the site uses the dev ' +
          "backend, and docs/shared/env.js doesn't have the dev Firebase project's config yet." +
          (env.local ? " To use production data here instead, open this page with ?env=prod." : "") + "</p>" +
      "</div></div>");
    return;
  }
  let formEl = null;

  function showForm(errorMessage) {
    if (formEl) {
      const err = formEl.querySelector(".login-error");
      if (err) err.textContent = errorMessage || "";
      return;
    }
    formEl = document.createElement("div");
    formEl.className = "login-gate";
    formEl.innerHTML =
      '<form class="login-form">' +
        '<div class="login-eyebrow">THE BACKLOG</div>' +
        '<h1 class="login-title">Sign in</h1>' +
        '<div class="field">' +
          '<label for="login-email">Email</label>' +
          '<input type="email" id="login-email" autocomplete="username" required>' +
        "</div>" +
        '<div class="field">' +
          '<label for="login-password">Password</label>' +
          '<input type="password" id="login-password" autocomplete="current-password" required>' +
        "</div>" +
        '<button type="submit" class="btn primary login-submit">Sign in</button>' +
        '<div class="login-error" role="alert"></div>' +
      "</form>";
    mountEl.appendChild(formEl);

    formEl.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      const email = formEl.querySelector("#login-email").value.trim();
      const password = formEl.querySelector("#login-password").value;
      const submitBtn = formEl.querySelector(".login-submit");
      submitBtn.disabled = true;
      signInWithEmailAndPassword(auth, email, password)
        .catch(function () {
          showForm("Sign-in failed — check your email and password.");
        })
        .finally(function () {
          submitBtn.disabled = false;
        });
    });
  }

  function hideForm() {
    if (formEl) {
      formEl.remove();
      formEl = null;
    }
  }

  onAuthStateChanged(auth, function (user) {
    if (user) {
      hideForm();
      onSignedIn(user);
    } else {
      showForm();
    }
  });
}
