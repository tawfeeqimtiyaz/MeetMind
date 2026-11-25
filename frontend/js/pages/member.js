/* Member home: stats, my tasks, my meetings. (Admins can open it too.) */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, icon, badge, empty, loading, formatDateTime, plural, api, toast } = M;

  const user = await M.requireUser();
  document.getElementById("greeting").textContent = "Good to see you, " + (user.name || user.email).split(" ")[0];

  async function loadStats() {
    try {
      const { stats } = await api.get("/dashboard");
      const mine = user.role === "admin" ? null : stats;
      document.querySelectorAll("[data-stat]").forEach((el) => { el.textContent = mine ? mine[el.dataset.stat] : "–"; });
      const count = document.getElementById("nav-task-count");
      if (mine && mine.open_tasks) { count.textContent = mine.open_tasks; count.hidden = false; } else { count.hidden = true; }
    } catch (err) { toast(err.message, "error"); }
  }

  const taskList = document.getElementById("task-list");
  const showDone = document.getElementById("show-done");

  async function loadTasks() {
    replace(taskList, M.skeleton(3));
    try {
      const { items } = await api.get("/tasks/mine");
      const visible = showDone.checked ? items : items.filter((t) => t.status !== "done");
      if (!visible.length) {
        replace(taskList, empty(items.length ? "All done. Nice work!" : "No tasks assigned to you yet.",
          items.length ? null : "Tasks appear here once an admin approves them after a meeting.", { icon: "i-tasks" }));
        return;
      }
      replace(taskList, visible.map((t) => M.taskRow(t, { canToggle: true, showMeeting: true, onChange: refresh })));
    } catch (err) { M.showPageError(taskList, err); }
  }

  function refresh() {
    loadStats();
    if (!showDone.checked) setTimeout(loadTasks, 600); // let the user see the tick before it disappears
  }
  showDone.addEventListener("change", loadTasks);

  async function loadMeetings() {
    const list = document.getElementById("meeting-list");
    replace(list, M.skeleton(3));
    try {
      const { items } = await api.get("/meetings?limit=50");
      if (!items.length) {
        replace(list, empty("No meetings yet.", "Meetings you’re invited to show up here.", { icon: "i-calendar" }));
        return;
      }
      replace(list, items.map((m) =>
        h("a", { class: "list-item", href: "meeting.html?id=" + encodeURIComponent(m.id) },
          h("div", {},
            h("div", { class: "title" }, m.title),
            h("div", { class: "meta" },
              h("span", {}, icon("i-clock"), formatDateTime(m.scheduled_at)),
              h("span", {}, icon("i-users"), plural(m.participant_count, "participant")),
            ),
          ),
          badge(m.status === "review" ? "processing" : m.status), // members see "review" as still processing
        )));
    } catch (err) { M.showPageError(list, err); }
  }

  await Promise.all([loadStats(), loadTasks(), loadMeetings()]);
})();
