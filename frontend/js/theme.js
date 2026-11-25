/* Light / dark theme. Loaded in <head> so the saved theme applies before the page paints (no flash).
   "system" (the default) follows the operating system; the toggle cycles system -> light -> dark. */
(function () {
  "use strict";
  const KEY = "meetmind.theme";
  const root = document.documentElement;

  function saved() {
    try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; }
  }

  function apply(theme) {
    if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
  }

  function effective() {
    const t = saved();
    if (t !== "system") return t;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  apply(saved());

  const LABEL = { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" };
  const ICON = { system: "◐", light: "☀", dark: "☾" };

  function makeButton(withLabel) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = withLabel ? "btn btn-ghost btn-sm theme-toggle theme-row" : "btn btn-ghost btn-icon theme-toggle";
    const update = () => {
      const t = saved();
      btn.textContent = withLabel ? ICON[t] + "  " + LABEL[t].replace("Theme: system", "Theme: same as device") : ICON[t];
      btn.setAttribute("aria-label", LABEL[t] + " (click to change)");
      btn.title = LABEL[t];
    };
    btn.addEventListener("click", () => {
      const next = { system: "light", light: "dark", dark: "system" }[saved()];
      try { localStorage.setItem(KEY, next); } catch (e) { /* private mode: still switch for this page */ }
      apply(next);
      update();
      document.querySelectorAll(".theme-toggle").forEach((b) => { if (b !== btn) b.dispatchEvent(new Event("sync")); });
      document.dispatchEvent(new CustomEvent("meetmind:theme", { detail: { theme: effective() } }));
    });
    btn.addEventListener("sync", update);
    update();
    return btn;
  }

  document.addEventListener("DOMContentLoaded", () => {
    // signed-in pages: next to "sign out" and in the mobile top bar; public pages: in the header
    const footer = document.querySelector(".sidebar-footer");
    if (footer) footer.prepend(makeButton(true));
    const topbar = document.querySelector(".topbar");
    if (topbar) topbar.insertBefore(makeButton(), topbar.lastElementChild);
    const nav = document.querySelector(".site-header nav");
    if (nav) nav.prepend(makeButton());
    const auth = document.querySelector(".auth-page");
    if (auth && !nav) {
      const b = makeButton();
      b.classList.add("theme-toggle-corner");
      document.body.append(b);
    }
  });

  window.addEventListener("storage", (e) => { if (e.key === KEY) { apply(saved()); document.querySelectorAll(".theme-toggle").forEach((b) => b.dispatchEvent(new Event("sync"))); } });
  window.MeetMindTheme = { effective };
})();
