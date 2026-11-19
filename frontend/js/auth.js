/* Login and accept-invite pages. Requires api.js. */
(function () {
  "use strict";

  const { api, saveSession, clearSession, homeFor } = window.MeetMind;

  // After login, go back to the page that sent us here, but only to one of our own pages
  function destination(user) {
    const next = new URLSearchParams(window.location.search).get("next") || "";
    return /^[a-z-]+\.html(\?[\w=&%.-]*)?$/.test(next) && !next.startsWith("login") ? next : homeFor(user);
  }

  function showError(el, message) {
    el.textContent = message;
    el.hidden = false;
    el.focus();
  }

  function setBusy(button, busy, busyLabel) {
    if (busy) {
      button.dataset.label = button.textContent;
      button.textContent = busyLabel;
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
    }
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
  }

  function handle(form, errorEl, busyLabel, submit) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      errorEl.hidden = true;
      if (!form.reportValidity()) return;
      const button = form.querySelector('button[type="submit"]');
      setBusy(button, true, busyLabel);
      try {
        const session = await submit(new FormData(form));
        saveSession(session);
        window.location.assign(destination(session.user));
      } catch (err) {
        setBusy(button, false);
        showError(errorEl, err.message);
      }
    });
  }

  // ---------- login.html ----------
  const loginForm = document.querySelector("form[data-login]");
  if (loginForm) {
    const params = new URLSearchParams(window.location.search);
    if (params.has("signout")) {
      clearSession();
      window.history.replaceState(null, "", "login.html");
    } else if (window.MeetMind.currentUser() && !params.has("next")) {
      window.location.replace(homeFor(window.MeetMind.currentUser())); // already signed in
      return;
    }
    const errorEl = document.getElementById("login-error");
    handle(loginForm, errorEl, "Signing in…", (data) =>
      api.post("/auth/login", { email: data.get("email"), password: data.get("password") })
    );
  }

  // ---------- signup.html ----------
  const signupForm = document.querySelector("form[data-signup]");
  if (signupForm) {
    handle(signupForm, document.getElementById("signup-error"), "Creating workspace…", (data) =>
      api.post("/auth/signup", {
        organization_name: data.get("organization_name"),
        name: data.get("name"),
        email: data.get("email"),
        password: data.get("password"),
      })
    );
  }

  // ---------- accept-invite.html ----------
  const inviteForm = document.querySelector("form[data-accept-invite]");
  if (inviteForm) {
    const errorEl = document.getElementById("invite-error");
    const token = new URLSearchParams(window.location.search).get("token");
    if (!token) {
      showError(errorEl, "This page needs the link from your invite email. Ask your admin to resend the invite.");
      inviteForm.querySelectorAll("input, button").forEach((el) => { el.disabled = true; });
    } else {
      // Keep the token out of the address bar (and browser history) once read
      window.history.replaceState(null, "", "accept-invite.html");
      handle(inviteForm, errorEl, "Joining…", (data) =>
        api.post("/auth/accept-invite", {
          token,
          name: data.get("name") || null,
          password: data.get("password"),
        }).catch((err) => {
          if (err.code === "invalid_invite") {
            err.message = "This invite link is invalid, already used or expired. Ask your admin to resend the invite.";
          }
          throw err;
        })
      );
    }
  }
})();
