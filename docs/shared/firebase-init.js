// Firebase setup shared by every page on this site (homepage, games section, and any
// future media-type sections). This config object is NOT a secret -- Firebase's access
// control lives entirely in Firestore Security Rules + Authentication, not in hiding this
// object, so it's safe to ship in the public bundle. See docs/../Source (rules published
// via the Firebase console) for the actual access boundary.
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.18.0/firebase-app.js";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
} from "https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js";
import {
  getFirestore,
  doc,
  setDoc,
  onSnapshot,
} from "https://www.gstatic.com/firebasejs/12.18.0/firebase-firestore.js";

const firebaseConfig = {
  apiKey: "AIzaSyD6xJX12ovVZ7Hi8Xtwu0L3CHmgKB3-BzY",
  authDomain: "the-backlog-34b22.firebaseapp.com",
  projectId: "the-backlog-34b22",
  storageBucket: "the-backlog-34b22.firebasestorage.app",
  messagingSenderId: "306665266277",
  appId: "1:306665266277:web:129933a71722256ba9ba8c",
};

const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getFirestore(app);
export { doc, setDoc, onSnapshot };

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
