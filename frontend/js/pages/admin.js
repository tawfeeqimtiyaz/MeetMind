/* Admin dashboard: stats, meetings awaiting review, all meetings, team management. */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, icon, badge, empty, loading, formatDateTime, plural, api, toast } = M;

  // Instant meeting: create a meeting that starts now and open it ready to record
  const instantBtn = document.getElementById("instant-meeting");
  instantBtn.addEventListener("click", async () => {
    const title = await M.dialog({
      title: "Record a meeting now",
      body: "A meeting starting now is created and opened, ready to record.",
      input: { label: "Meeting title", value: "Meeting " + new Date().toLocaleString([], { dateStyle: "medium", timeStyle: "short" }), maxlength: 200 },
      confirmLabel: "Create & open recorder",
    });
    if (title === null) return;
    instantBtn.disabled = true;
    try {
      const { meeting } = await api.post("/meetings/instant", {
        title: title || null,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
      });
      location.assign("meeting.html?id=" + encodeURIComponent(meeting.id) + "&record=1");
    } catch (err) {
      instantBtn.disabled = false;
      toast(err.message, "error");
    }
  });

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

  /* ---------- stats, needs attention, upcoming, charts (one request) ---------- */
  const ATTENTION = {
    live: ["●", "Open"], failed: ["!", "Retry"], review: ["✎", "Review"], starred: ["★", "Open"],
    overdue: ["!", "View"], urgent: ["⚡", "View"],
  };

  function attentionList(ins) {
    const items = [...ins.attention];
    if (ins.overdue_tasks) items.splice(0, 0, { kind: "overdue", href: "tasks.html?due=overdue",
      title: plural(ins.overdue_tasks, "overdue task"), detail: "Past their due date and not done yet" });
    if (ins.urgent_tasks) items.splice(1, 0, { kind: "urgent", href: "tasks.html?priority=urgent",
      title: plural(ins.urgent_tasks, "urgent task") + " open", detail: "Marked urgent or ASAP" });
    if (!items.length) {
      return h("div", { class: "all-clear" }, "✓", h("span", {}, "All clear. Nothing needs your attention right now."));
    }
    return h("ul", { class: "attention" }, items.map((a) => {
      const [glyph, action] = ATTENTION[a.kind] || ["•", "Open"];
      return h("li", {}, h("a", { class: "attention-item", href: a.href || "meeting.html?id=" + encodeURIComponent(a.meeting_id) + (a.kind === "review" ? "#tasks" : "") },
        h("span", { class: "attention-icon k-" + a.kind, "aria-hidden": "true" }, glyph),
        h("span", {}, h("span", { class: "attention-title" }, a.title), h("span", { class: "muted small", style: "display:block" }, a.detail)),
        h("span", { class: "btn btn-ghost btn-sm", "aria-hidden": "true" }, action, " →")));
    }));
  }

  function upcomingList(items) {
    if (!items.length) return empty("Nothing scheduled this week.", null, { icon: "i-calendar", action: { label: "Schedule a meeting", href: "create-meeting.html" } });
    return h("ul", { class: "upcoming" }, items.map((m) => {
      const d = new Date(m.scheduled_at);
      return h("li", {}, h("a", { href: "meeting.html?id=" + encodeURIComponent(m.id) },
        h("span", { class: "date-chip", "aria-hidden": "true" },
          h("span", { class: "d" }, d.getDate()), h("span", { class: "m" }, d.toLocaleDateString([], { month: "short" }))),
        h("span", {}, h("span", { class: "attention-title" }, m.title),
          h("span", { class: "muted small", style: "display:block" },
            d.toLocaleString([], { weekday: "long", hour: "2-digit", minute: "2-digit" }) + " · " + plural(m.participant_count, "participant")))));
    }));
  }

  // Tasks by status: one segmented bar (part of a whole) + legend with counts
  function statusChart(byStatus) {
    const parts = [["open", "To do", "c1"], ["in_progress", "In progress", "c2"], ["done", "Done", "c3"]];
    const total = parts.reduce((n, [k]) => n + (byStatus[k] || 0), 0);
    const pct = (n) => (total ? Math.round((n / total) * 100) : 0);
    return h("div", { class: "chart-block" },
      h("div", { class: "chart-label" }, h("strong", {}, "Tasks by status"), h("span", {}, plural(total, "approved task"))),
      h("div", { class: "seg-bar", role: "img", "aria-label": parts.map(([k, label]) => `${label} ${byStatus[k] || 0}`).join(", ") },
        total ? parts.filter(([k]) => byStatus[k]).map(([k, label, c]) =>
          h("span", { class: "seg " + c, style: `flex-grow:${byStatus[k]}`, title: `${label}: ${byStatus[k]} (${pct(byStatus[k])}%)` }))
          : h("span", { class: "seg-empty", title: "No approved tasks yet" })),
      h("div", { class: "legend" }, parts.map(([k, label, c]) =>
        h("span", {}, h("span", { class: "swatch " + c }), label, h("strong", {}, String(byStatus[k] || 0)), h("span", { class: "muted" }, `(${pct(byStatus[k] || 0)}%)`)))));
  }

  // Open work per person: bar = open tasks, the red end = overdue part
  function workloadChart(rows) {
    if (!rows.length) return h("div", { class: "chart-block" }, h("div", { class: "chart-label" }, h("strong", {}, "Open work per person")),
      h("p", { class: "muted small" }, "No open tasks. 🎉"));
    const max = Math.max(...rows.map((r) => r.open));
    return h("div", { class: "chart-block" },
      h("div", { class: "chart-label" }, h("strong", {}, "Open work per person"), h("span", {}, "top " + rows.length)),
      h("div", { class: "workload" }, rows.map((r) => {
        const onTime = r.open - r.overdue;
        const label = `${r.name}: ${plural(r.open, "open task")}` + (r.overdue ? `, ${r.overdue} overdue` : "") + (r.urgent ? `, ${r.urgent} urgent` : "");
        return h("div", { class: "wl-row", title: label },
          h("span", { class: "wl-name" }, r.name),
          h("span", { class: "wl-track", role: "img", "aria-label": label },
            onTime ? h("span", { class: "seg c1", style: `width:${(onTime / max) * 100}%` }) : null,
            r.overdue ? h("span", { class: "seg c-danger", style: `width:${(r.overdue / max) * 100}%` }) : null),
          h("span", { class: "wl-value" }, String(r.open), r.overdue ? h("span", { class: "text-danger" }, ` · ${r.overdue} late`) : null));
      })),
      h("div", { class: "legend" },
        h("span", {}, h("span", { class: "swatch c1" }), "Open, on time"),
        h("span", {}, h("span", { class: "swatch c-danger" }), "Overdue")));
  }

  async function loadDashboard() {
    const attention = document.getElementById("attention");
    replace(attention, M.skeleton(3));
    replace(document.getElementById("upcoming"), M.skeleton(2));
    replace(document.getElementById("charts"), M.skeleton(2));
    try {
      const { stats, insights } = await api.get("/dashboard");
      document.querySelectorAll("[data-stat]").forEach((el) => { el.textContent = stats[el.dataset.stat] ?? "–"; });
      replace(attention, attentionList(insights));
      replace(document.getElementById("upcoming"), upcomingList(insights.upcoming));
      replace(document.getElementById("charts"), statusChart(insights.tasks_by_status), workloadChart(insights.workload));
    } catch (err) {
      M.showPageError(attention, err);
    }
  }

  /* ---------- all meetings (paged) ---------- */
  const PAGE = 20;
  let offset = 0;
  const listEl = document.getElementById("meeting-list");
  const moreBtn = document.getElementById("more-meetings");
  const filter = document.getElementById("status-filter");

  async function loadMeetings(reset) {
    if (reset) { offset = 0; replace(listEl, M.skeleton(4)); }
    const status = filter.value ? "&status=" + encodeURIComponent(filter.value) : "";
    try {
      const { items, total } = await api.get(`/meetings?limit=${PAGE}&offset=${offset}${status}`);
      if (reset) listEl.replaceChildren();
      if (total === 0) {
        replace(listEl, empty(filter.value ? "No meetings with this status." : "No meetings yet.",
          filter.value ? null : "Create your first meeting, or record one right now.",
          filter.value ? { icon: "i-calendar" } : { icon: "i-calendar", action: { label: "New meeting", href: "create-meeting.html" } }));
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
        onclick: async () => {
          if (disable && !(await M.confirmDialog({ title: `Disable ${u.email}?`, body: "They will be signed out and lose access.", confirmLabel: "Disable", danger: true }))) return;
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

  await Promise.all([loadDashboard(), loadMeetings(true), loadTeam()]);
})();
