/* Admin dashboard: stats, meetings awaiting review, all meetings, team management. */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, icon, badge, empty, loading, formatDateTime, plural, api, toast } = M;

  const user = await M.requireUser({ admin: true });
  document.getElementById("greeting").textContent = "Welcome, " + (user.name || user.email).split(" ")[0];

  function meetingRow(m, extra) {
    return h("a", { class: "list-item", href: "meeting.html?id=" + encodeURIComponent(m.id) },
      h("div", {},
        h("div", { class: "title" }, m.title),
        h("div", { class: "meta" },
          h("span", {}, icon("i-clock"), formatDateTime(m.scheduled_at)),
          h("span", {}, icon("i-users"), plural(m.participant_count, "participant")),
          extra ? h("span", { class: "meta-strong" }, extra) : null,
        ),
      ),
      badge(m.status),
    );
  }

  /* ---------- stats ---------- */
  async function loadStats() {
    try {
      const { stats } = await api.get("/dashboard");
      document.querySelectorAll("[data-stat]").forEach((el) => { el.textContent = stats[el.dataset.stat] ?? "–"; });
    } catch (err) { toast(err.message, "error"); }
  }

  /* ---------- needs review ---------- */
  async function loadReview() {
    const section = document.getElementById("review");
    const list = document.getElementById("review-list");
    try {
      const { items } = await api.get("/meetings?status=review&limit=20");
      section.hidden = items.length === 0;
      replace(list, items.map((m) => meetingRow(m, plural(m.draft_task_count, "task") + " drafted by AI")));
    } catch (err) { toast(err.message, "error"); }
  }

  /* ---------- all meetings (paged) ---------- */
  const PAGE = 20;
  let offset = 0;
  const listEl = document.getElementById("meeting-list");
  const moreBtn = document.getElementById("more-meetings");
  const filter = document.getElementById("status-filter");

  async function loadMeetings(reset) {
    if (reset) { offset = 0; replace(listEl, loading()); }
    const status = filter.value ? "&status=" + encodeURIComponent(filter.value) : "";
    try {
      const { items, total } = await api.get(`/meetings?limit=${PAGE}&offset=${offset}${status}`);
      if (reset) listEl.replaceChildren();
      if (total === 0) {
        replace(listEl, empty(filter.value ? "No meetings with this status." : "No meetings yet.",
          filter.value ? null : "Create your first meeting, then upload its recording."));
      } else {
        listEl.append(...items.map((m) => meetingRow(m)));
      }
      offset += items.length;
      moreBtn.hidden = offset >= total;
    } catch (err) { M.showPageError(listEl, err); }
  }
  filter.addEventListener("change", () => loadMeetings(true));
  moreBtn.addEventListener("click", () => loadMeetings(false));

  /* ---------- team ---------- */
  const teamEl = document.getElementById("team-list");
  const STATUS_LABEL = { active: "Active", invited: "Invite sent", disabled: "Disabled" };

  function teamRow(u) {
    const isMe = u.id === user.id;
    const actions = h("div", { class: "row-actions" });
    if (!isMe && u.status === "invited") {
      actions.append(h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => run(
        () => api.post(`/users/${u.id}/resend-invite`), "Invite re-sent to " + u.email) }, "Resend invite"));
    }
    if (!isMe && u.status !== "invited") {
      const disable = u.status === "active";
      actions.append(h("button", { class: "btn btn-ghost btn-sm" + (disable ? " text-danger" : ""), type: "button",
        onclick: () => {
          if (disable && !confirm(`Disable ${u.email}? They will be signed out and lose access.`)) return;
          run(() => api.patch(`/users/${u.id}`, { status: disable ? "disabled" : "active" }),
            (disable ? "Disabled " : "Re-enabled ") + u.email);
        } }, disable ? "Disable" : "Enable"));
    }
    return h("div", { class: "list-item list-item-static" + (u.status === "disabled" ? " is-muted" : "") },
      h("span", { class: "avatar", "aria-hidden": "true" }, M.initials(u.name || u.email)),
      h("div", {},
        h("div", { class: "title" }, u.name || u.email, isMe ? h("span", { class: "muted" }, " (you)") : null),
        h("div", { class: "meta" }, h("span", {}, u.email), h("span", {}, u.role === "admin" ? "Admin" : "Member")),
      ),
      h("span", { class: "badge " + (u.status === "active" ? "badge-completed" : u.status === "invited" ? "badge-review" : "badge-failed") },
        STATUS_LABEL[u.status] || u.status),
      actions,
    );
  }

  async function loadTeam() {
    try {
      const { items } = await api.get("/users");
      replace(teamEl, items.map(teamRow));
    } catch (err) { M.showPageError(teamEl, err); }
  }

  async function run(action, message) {
    try {
      await action();
      toast(message);
      loadTeam();
    } catch (err) { toast(err.message, "error"); }
  }

  const inviteForm = document.getElementById("invite-form");
  inviteForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!inviteForm.reportValidity()) return;
    const data = new FormData(inviteForm);
    const button = inviteForm.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api.post("/users/invite", {
        email: data.get("email"), name: data.get("name") || null, role: data.get("role"),
      });
      toast("Invite sent to " + data.get("email"));
      inviteForm.reset();
      loadTeam();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  await Promise.all([loadStats(), loadReview(), loadMeetings(true), loadTeam()]);
})();
