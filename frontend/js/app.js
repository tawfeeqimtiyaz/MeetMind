/* Helpers shared by the signed-in pages: auth guard, safe DOM building, formatting.
   Rule: API data is only ever inserted with textContent (via h()), never innerHTML. */
(function () {
  "use strict";

  const M = window.MeetMind;

  /* ---------- DOM ---------- */

  // h("a", {class: "x", href: url, onclick: fn}, "text", childNode, [more])
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "class") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, value);
    }
    append(el, children);
    return el;
  }

  function append(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function icon(name, extraClass) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon" + (extraClass ? " " + extraClass : ""));
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "assets/icons.svg#" + name);
    svg.append(use);
    return svg;
  }

  function replace(el, ...children) {
    el.replaceChildren();
    return append(el, children);
  }

  // empty("No meetings yet.", "Create one…", {icon: "i-calendar", action: {label, href | onclick}})
  function empty(text, sub, opts) {
    const o = opts || {};
    const action = o.action
      ? (o.action.href ? h("a", { class: "btn btn-secondary btn-sm", href: o.action.href }, o.action.label)
        : h("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: o.action.onclick }, o.action.label))
      : null;
    return h("div", { class: "empty-state" },
      o.icon ? icon(o.icon, "empty-icon") : null,
      h("p", { class: "empty-title" }, text), sub ? h("p", { class: "muted small" }, sub) : null, action);
  }

  function loading(text) {
    return h("div", { class: "loading", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), text || "Loading…");
  }

  // Grey placeholder rows while a list loads (feels faster than a spinner)
  function skeleton(rows) {
    const widths = ["62%", "48%", "70%", "55%", "40%"];
    return h("div", { class: "skeleton", role: "status", "aria-label": "Loading" },
      Array.from({ length: rows || 3 }, (_, i) => h("div", { class: "skeleton-row" },
        h("div", { class: "skeleton-line is-title", style: "--w:" + widths[i % widths.length] }),
        h("div", { class: "skeleton-line", style: "--w:" + widths[(i + 2) % widths.length] }))));
  }

  // Minimal, safe Markdown for AI answers: paragraphs, "- " / "1. " lists, **bold**, `code`.
  // Builds DOM nodes from text (never innerHTML), so model output can't inject markup.
  // options.cite(n) -> href turns "[1]" source citations into links (used by Ask MeetMind)
  function markdownLite(text, options) {
    const cite = options && options.cite;
    const pattern = cite ? /(\*\*[^*]+\*\*|`[^`]+`|\[\d{1,2}\])/g : /(\*\*[^*]+\*\*|`[^`]+`)/g;
    const inline = (line) => line.split(pattern).filter(Boolean).map((part) => {
      if (part.startsWith("**") && part.endsWith("**")) return h("strong", {}, part.slice(2, -2));
      if (part.startsWith("`") && part.endsWith("`")) return h("code", {}, part.slice(1, -1));
      if (cite && /^\[\d{1,2}\]$/.test(part)) {
        const n = Number(part.slice(1, -1));
        const href = cite(n);
        return href ? h("a", { class: "cite", href, title: "Source " + n }, String(n)) : part;
      }
      return part;
    });
    const out = [];
    let list = null;
    for (const raw of String(text).split(/\r?\n/)) {
      const line = raw.trim();
      const item = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (item) {
        if (!list) { list = h("ul"); out.push(list); }
        list.append(h("li", {}, inline(item[1])));
      } else if (line) {
        list = null;
        out.push(h("p", {}, inline(line)));
      } else {
        list = null;
      }
    }
    return out;
  }

  /* ---------- formatting ---------- */

  const STATUS = {
    scheduled: ["Scheduled", "badge-scheduled"],
    processing: ["Processing", "badge-processing"],
    queued: ["Queued", "badge-processing"],
    review: ["Review", "badge-review"],
    recording: ["Recording", "badge-recording"],
    completed: ["Completed", "badge-completed"],
    failed: ["Failed", "badge-failed"],
    done: ["Done", "badge-done"],
    open: ["Open", "badge-scheduled"],
    in_progress: ["In progress", "badge-processing"],
  };

  function badge(status) {
    const [label, cls] = STATUS[status] || [status, "badge-scheduled"];
    return h("span", { class: "badge " + cls }, label);
  }

  function formatDateTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  // "2026-12-04" is a calendar date: format it without shifting through time zones
  function formatDate(ymd) {
    if (!ymd) return "";
    const [y, m, d] = ymd.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }

  function isOverdue(ymd) {
    if (!ymd) return false;
    const [y, m, d] = ymd.split("-").map(Number);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return new Date(y, m - 1, d) < today;
  }

  function initials(text) {
    const parts = String(text || "?").split(/[\s@._-]+/).filter(Boolean);
    return ((parts[0] || "?")[0] + (parts.length > 1 ? parts[1][0] : "")).toUpperCase();
  }

  function personName(person) {
    if (!person) return "Unassigned";
    return person.name || person.email;
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  /* ---------- auth guard ---------- */

  // Call at the top of every signed-in page. Resolves with the fresh user, or redirects.
  async function requireUser(options) {
    const opts = options || {};
    const here = location.pathname.split("/").pop() + location.search;
    if (!M.currentUser()) {
      location.replace("login.html?next=" + encodeURIComponent(here));
      return new Promise(() => {});
    }
    try {
      const { user } = await M.api.get("/auth/me");
      M.saveSession({ token: M.token(), user });
      if (opts.admin && user.role !== "admin") {
        location.replace("member.html");
        return new Promise(() => {});
      }
      setNav(user);
      M.showSignedInUser();
      return user;
    } catch (err) {
      if (err.status === 401) {
        location.replace("login.html?next=" + encodeURIComponent(here));
        return new Promise(() => {});
      }
      throw err;
    }
  }

  // Sidebar links depend on the role (members have no "New meeting")
  function setNav(user) {
    document.querySelectorAll("[data-admin-only]").forEach((el) => { el.hidden = user.role !== "admin"; });
    document.querySelectorAll("[data-member-only]").forEach((el) => { el.hidden = user.role === "admin"; });
    document.querySelectorAll("[data-home-link]").forEach((el) => { el.setAttribute("href", M.homeFor(user)); });
  }

  function showPageError(container, err) {
    replace(container, h("div", { class: "alert alert-error", role: "alert" }, err.message || "Something went wrong."));
  }

  /* ---------- "recording in another tab" banner ---------- */
  // The meeting page that is recording writes a heartbeat to localStorage every few seconds.
  // Every other signed-in tab shows a banner, so nobody forgets a meeting is being recorded.
  const LIVE_KEY = "meetmind.live";

  function readLive() {
    try {
      const live = JSON.parse(localStorage.getItem(LIVE_KEY) || "null");
      return live && Date.now() - live.at < 20000 ? live : null; // stale heartbeat: that tab is gone
    } catch (e) { return null; }
  }

  function renderLiveBanner() {
    const host = document.querySelector(".app .main-inner");
    if (!host) return;
    let banner = document.getElementById("live-banner");
    const live = M.recordingHere ? null : readLive();
    if (!live) { if (banner) banner.remove(); return; }
    if (!banner) {
      banner = h("div", { class: "live-banner", id: "live-banner", role: "status" });
      host.prepend(banner);
    }
    replace(banner, h("span", { class: "rec-dot", "aria-hidden": "true" }),
      h("span", {}, h("strong", {}, "Recording in progress: "), live.title || "a meeting",
        " · in another tab. Keep that tab open until you press Stop."),
      h("a", { href: "meeting.html?id=" + encodeURIComponent(live.id) }, "View meeting"));
  }

  M.liveHeartbeat = function (meetingId, title) {
    try { localStorage.setItem(LIVE_KEY, JSON.stringify({ id: meetingId, title, at: Date.now() })); } catch (e) { /* ignore */ }
  };
  M.liveClear = function () {
    try { localStorage.removeItem(LIVE_KEY); } catch (e) { /* ignore */ }
  };
  window.addEventListener("storage", (e) => { if (e.key === LIVE_KEY) renderLiveBanner(); });
  document.addEventListener("DOMContentLoaded", () => { renderLiveBanner(); setInterval(renderLiveBanner, 10000); });

  // Every form on the signed-in pages is handled by JavaScript. Never let one submit natively,
  // e.g. when Enter is pressed before the page script has finished loading (that would reload the page).
  document.addEventListener("submit", (e) => {
    if (e.target.closest(".app")) e.preventDefault();
  });

  Object.assign(M, {
    h, append, replace, icon, empty, loading, skeleton, badge, markdownLite,
    formatDateTime, formatDate, isOverdue, initials, personName, plural,
    requireUser, showPageError,
  });
})();
