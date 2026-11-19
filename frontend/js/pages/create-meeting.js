/* New meeting form -> POST /api/meetings, then open the meeting. */
(async function () {
  "use strict";
  const M = window.MeetMind;
  await M.requireUser({ admin: true });

  const form = document.getElementById("meeting-form");
  const errorEl = document.getElementById("form-error");
  const when = document.getElementById("scheduled_at");

  // Show the time zone so the scheduled time is unambiguous; default to the next full hour
  document.getElementById("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  if (!when.value) {
    const d = new Date();
    d.setHours(d.getHours() + 1, 0, 0, 0);
    when.value = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }

  const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    if (!form.reportValidity()) return;

    const data = new FormData(form);
    const emails = String(data.get("participants") || "").split(/[\s,;]+/).map((s) => s.trim().toLowerCase()).filter(Boolean);
    const invalid = emails.filter((x) => !EMAIL.test(x));
    if (invalid.length) {
      errorEl.textContent = "These don’t look like email addresses: " + invalid.join(", ");
      errorEl.hidden = false;
      errorEl.focus();
      return;
    }

    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const { meeting } = await M.api.post("/meetings", {
        title: data.get("title"),
        agenda: data.get("agenda") || null,
        // datetime-local is the user's local time; send an exact UTC instant
        scheduled_at: new Date(data.get("scheduled_at")).toISOString(),
        // the meeting's local time zone, so "by Friday" is resolved on the right calendar day
        timezone: document.getElementById("timezone").value || null,
        participants: [...new Set(emails)],
      });
      location.assign("meeting.html?id=" + encodeURIComponent(meeting.id) + "&created=1");
    } catch (err) {
      button.disabled = false;
      errorEl.textContent = err.message;
      errorEl.hidden = false;
      errorEl.focus();
    }
  });
})();
