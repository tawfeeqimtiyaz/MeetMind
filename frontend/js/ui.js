/* MeetMind — shared UI behaviour (no framework).
   Pages opt in with data-* attributes, so each page stays plain HTML. */
(function () {
  "use strict";

  /* ---------- Toasts ---------- */
  function toast(message, type) {
    let region = document.querySelector(".toast-region");
    if (!region) {
      region = document.createElement("div");
      region.className = "toast-region";
      region.setAttribute("role", "status");
      region.setAttribute("aria-live", "polite");
      document.body.appendChild(region);
    }
    const el = document.createElement("div");
    el.className = "toast" + (type === "error" ? " toast-error" : type === "success" ? " toast-success" : "");
    if (type === "error" || type === "success") {
      const ic = document.createElement("span");
      ic.className = "toast-icon";
      ic.setAttribute("aria-hidden", "true");
      ic.textContent = type === "error" ? "!" : "✓";
      el.appendChild(ic);
    }
    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = message; // textContent: never inject HTML
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => el.remove());
    el.append(text, close);
    region.appendChild(el);
    setTimeout(() => el.remove(), type === "error" ? 7000 : 4000);
  }

  /* ---------- Dialogs (instead of the browser's confirm() / prompt()) ---------- */
  // dialog({title, body, confirmLabel, cancelLabel, danger, input: {label, value, maxlength}})
  // resolves: true / the typed text when confirmed, null when cancelled
  function dialog(opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.className = "dialog";
      const form = document.createElement("form");
      form.method = "dialog";
      const title = document.createElement("h2");
      title.textContent = o.title || "Are you sure?";
      form.append(title);
      if (o.body) {
        const p = document.createElement("p");
        p.className = "dialog-body";
        p.textContent = o.body;
        form.append(p);
      }
      let input = null;
      if (o.input) {
        const field = document.createElement("label");
        field.className = "field";
        const label = document.createElement("span");
        label.className = "label";
        label.textContent = o.input.label || "";
        input = document.createElement("input");
        input.className = "input";
        input.value = o.input.value || "";
        input.maxLength = o.input.maxlength || 200;
        input.required = true;
        field.append(label, input);
        form.append(field);
      }
      const actions = document.createElement("div");
      actions.className = "dialog-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn btn-ghost";
      cancel.textContent = o.cancelLabel || "Cancel";
      const ok = document.createElement("button");
      ok.type = "submit";
      ok.className = "btn " + (o.danger ? "btn-danger-solid" : "btn-primary");
      ok.textContent = o.confirmLabel || "OK";
      actions.append(cancel, ok);
      form.append(actions);
      d.append(form);
      document.body.append(d);

      let result = null;
      cancel.addEventListener("click", () => d.close());
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        if (input) {
          const v = input.value.trim();
          if (!v) { input.focus(); return; }
          result = v;
        } else {
          result = true;
        }
        d.close();
      });
      d.addEventListener("close", () => { d.remove(); resolve(result); });
      d.showModal();
      if (input) { input.focus(); input.select(); } else { (o.danger ? cancel : ok).focus(); }
    });
  }
  const confirmDialog = (opts) => dialog(opts).then((r) => r === true);

  /* ---------- Mobile sidebar ---------- */
  function initNav() {
    const app = document.querySelector(".app");
    const toggle = document.querySelector("[data-nav-toggle]");
    if (!app || !toggle) return;

    const setOpen = (open) => {
      app.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", String(open));
    };
    toggle.addEventListener("click", () => setOpen(!app.classList.contains("nav-open")));
    app.addEventListener("click", (e) => {
      if (app.classList.contains("nav-open") && !e.target.closest(".sidebar, [data-nav-toggle]")) setOpen(false);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
  }

  /* ---------- Show / hide password ---------- */
  function initPasswordToggles() {
    document.querySelectorAll("[data-toggle-password]").forEach((btn) => {
      const input = document.getElementById(btn.dataset.togglePassword);
      if (!input) return;
      btn.addEventListener("click", () => {
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-pressed", String(show));
        btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      });
    });
  }

  /* ---------- Recording drop zone ---------- */
  function formatBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
  }

  function initDropzones() {
    document.querySelectorAll("[data-dropzone]").forEach((zone) => {
      const input = zone.querySelector('input[type="file"]');
      const chip = document.getElementById(zone.dataset.dropzone);
      const maxMb = Number(zone.dataset.maxMb || 500);

      const show = (file) => {
        if (!file || !chip) return;
        if (file.size > maxMb * 1024 * 1024) {
          toast(`That file is larger than ${maxMb} MB.`, "error");
          input.value = "";
          chip.hidden = true;
          return;
        }
        chip.querySelector(".file-name").textContent = file.name;
        chip.querySelector(".file-size").textContent = formatBytes(file.size);
        chip.hidden = false;
      };

      input.addEventListener("change", () => show(input.files[0]));
      ["dragenter", "dragover"].forEach((ev) =>
        zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("is-dragover"); }));
      ["dragleave", "drop"].forEach((ev) =>
        zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("is-dragover"); }));
      zone.addEventListener("drop", (e) => {
        if (e.dataTransfer.files.length) {
          input.files = e.dataTransfer.files;
          show(input.files[0]);
        }
      });
      zone.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
      });
    });
  }

  /* ---------- Footer year ---------- */
  function initYear() {
    document.querySelectorAll("[data-year]").forEach((el) => { el.textContent = new Date().getFullYear(); });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initNav();
    initPasswordToggles();
    initDropzones();
    initYear();
  });

  window.MeetMind = Object.assign(window.MeetMind || {}, { toast, formatBytes, dialog, confirmDialog });
})();
