/* MeetMind API client: fetch wrapper + login session (shared by every page). */
(function () {
  "use strict";

  const TOKEN_KEY = "meetmind.token";
  const USER_KEY = "meetmind.user";

  // Storage can be unavailable (private mode, blocked cookies): never let that break the page
  function load(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function store(key, value) {
    try {
      if (value == null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* ignore */ }
  }

  class ApiError extends Error {
    constructor(status, code, message, details) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details;
    }
  }

  async function request(method, path, body) {
    const headers = { Accept: "application/json" };
    const token = load(TOKEN_KEY);
    if (token) headers.Authorization = "Bearer " + token;

    let payload;
    if (body instanceof FormData) {
      payload = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }

    let res;
    try {
      res = await fetch("/api" + path, { method, headers, body: payload });
    } catch (e) {
      throw new ApiError(0, "network_error", "Can’t reach MeetMind. Check your connection and try again.");
    }
    if (res.status === 204) return null;

    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = data.error || {};
      if (res.status === 401 && token) clearSession(); // expired or revoked
      let message = err.message || "Something went wrong. Please try again.";
      if (err.code === "validation_error" && Array.isArray(err.details) && err.details.length) {
        message = err.details.map((d) => (d.field ? d.field.replace(/_/g, " ") + ": " : "") + d.message).join(" · ");
      }
      throw new ApiError(res.status, err.code || "error", message, err.details);
    }
    return data;
  }

  // Uploads use XMLHttpRequest because fetch() can't report upload progress
  function upload(path, formData, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api" + path);
      const token = load(TOKEN_KEY);
      if (token) xhr.setRequestHeader("Authorization", "Bearer " + token);
      xhr.setRequestHeader("Accept", "application/json");
      if (onProgress) {
        xhr.upload.addEventListener("progress", (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); });
      }
      xhr.addEventListener("load", () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
        const err = data.error || {};
        reject(new ApiError(xhr.status, err.code || "error", err.message ||
          (xhr.status === 413 ? "That file is too large." : "Upload failed. Please try again."), err.details));
      });
      xhr.addEventListener("error", () => reject(new ApiError(0, "network_error", "Upload failed: can’t reach MeetMind.")));
      xhr.send(formData);
    });
  }

  function saveSession(session) {
    store(TOKEN_KEY, session.token);
    store(USER_KEY, JSON.stringify(session.user));
  }

  function clearSession() {
    store(TOKEN_KEY, null);
    store(USER_KEY, null);
  }

  function currentUser() {
    try { return JSON.parse(load(USER_KEY)); } catch (e) { return null; }
  }

  function homeFor(user) {
    return user && user.role === "admin" ? "admin.html" : "member.html";
  }

  function initials(user) {
    const source = (user.name || user.email || "?").trim();
    const parts = source.split(/[\s@._-]+/).filter(Boolean);
    return ((parts[0] || "?")[0] + (parts.length > 1 ? parts[1][0] : "")).toUpperCase();
  }

  // Show the signed-in person in the app shell (sidebar chip + mobile top bar)
  function showSignedInUser() {
    const user = currentUser();
    if (!user) return;
    const role = (user.role === "admin" ? "Admin" : "Member") + (user.organization ? " · " + user.organization.name : "");
    document.querySelectorAll(".user-chip .name").forEach((el) => { el.textContent = user.name || user.email; });
    document.querySelectorAll(".user-chip .role").forEach((el) => { el.textContent = role; });
    document.querySelectorAll(".user-chip .avatar, .topbar .avatar").forEach((el) => { el.textContent = initials(user); });
  }

  window.MeetMind = Object.assign(window.MeetMind || {}, {
    api: {
      get: (path) => request("GET", path),
      post: (path, body) => request("POST", path, body),
      patch: (path, body) => request("PATCH", path, body),
      del: (path) => request("DELETE", path),
      upload,
    },
    token: () => load(TOKEN_KEY),
    ApiError,
    saveSession,
    clearSession,
    currentUser,
    homeFor,
    showSignedInUser,
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", showSignedInUser);
  else showSignedInUser();
})();
