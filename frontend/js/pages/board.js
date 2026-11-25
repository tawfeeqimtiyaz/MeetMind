/* Tasks board: To do / In progress / Done columns with filters.
   Admins see every approved task in the workspace, members only their own.
   Move a card by dragging it to another column, or with its status menu (keyboard & touch friendly). */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, api, toast, plural, formatDate, isOverdue, personName } = M;
  const $ = (id) => document.getElementById(id);

  const user = await M.requireUser();
  const isAdmin = user.role === "admin";
  if (!isAdmin) {
    $("board-title").textContent = "My tasks board";
    $("board-subtitle").textContent = "Your tasks from every meeting. Drag a card, or use its status menu, to move it.";
  }

  const COLUMNS = [["open", "To do"], ["in_progress", "In progress"], ["done", "Done"]];
  const DONE_PREVIEW = 15;
  const form = $("filters");
  const board = $("board");
  let tasks = [];
  let showAllDone = false;
  let dragging = null;

  /* ---------- filters (kept in the address, so the dashboard can link to "overdue") ---------- */
  const FIELDS = ["q", "priority", "assignee", "due"];
  const params = new URLSearchParams(location.search);
  FIELDS.forEach((f) => { if (params.get(f) && form.elements[f]) form.elements[f].value = params.get(f); });

  if (isAdmin) {
    $("f-assignee-field").hidden = false;
    try {
      const { items } = await api.get("/users");
      const wanted = params.get("assignee");
      items.filter((u) => u.status !== "disabled").forEach((u) =>
        $("f-assignee").append(h("option", { value: u.email, selected: u.email === wanted }, u.name || u.email)));
    } catch (e) { /* owner filter just stays short */ }
  }

  function query() {
    const q = new URLSearchParams();
    FIELDS.forEach((f) => {
      const el = form.elements[f];
      if (el && !el.closest("[hidden]") && el.value.trim()) q.set(f, el.value.trim());
    });
    return q;
  }

  let searchTimer = null;
  form.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(load, e.target.type === "search" ? 300 : 0);
  });
  form.addEventListener("reset", () => setTimeout(load, 0));
  form.addEventListener("submit", (e) => { e.preventDefault(); load(); });

  /* ---------- loading ---------- */
  async function load() {
    const q = query();
    history.replaceState(null, "", "tasks.html" + (q.toString() ? "?" + q : ""));
    if (!tasks.length) replace(board, COLUMNS.map(([, label]) => column(label, null, [M.skeleton(2)])));
    try {
      const res = await api.get("/tasks" + (q.toString() ? "?" + q : ""));
      tasks = res.items;
      const filtered = q.toString() !== "";
      $("board-status").textContent = plural(tasks.length, "task") + (filtered ? " match your filters" : "")
        + (res.limited ? " (showing the first 500; narrow the filters to see more)" : "");
      render();
    } catch (err) { M.showPageError(board, err); }
  }

  /* ---------- rendering ---------- */
  function column(label, status, children, count) {
    const col = h("section", { class: "board-col", "aria-label": label, dataset: status ? { status } : {} },
      h("div", { class: "board-col-head" }, label, count != null ? h("span", { class: "count" }, String(count)) : null),
      h("div", { class: "board-list" }, children));
    if (status) {
      col.addEventListener("dragover", (e) => { if (dragging) { e.preventDefault(); col.classList.add("is-over"); } });
      col.addEventListener("dragleave", (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("is-over"); });
      col.addEventListener("drop", (e) => {
        e.preventDefault();
        col.classList.remove("is-over");
        if (dragging && dragging.status !== status) move(dragging, status);
      });
    }
    return col;
  }

  function card(t) {
    const done = t.status === "done";
    const late = !done && isOverdue(t.due_date);
    const select = h("select", { class: "input select-status", "aria-label": "Status of " + t.title },
      COLUMNS.map(([value, label]) => h("option", { value, selected: value === t.status }, label)));
    select.addEventListener("change", () => move(t, select.value));
    const owner = t.assignee ? personName(t.assignee) : "Unassigned";
    const el = h("article", {
      class: "board-card" + (done ? " is-done" : "") + (t.priority === "urgent" ? " is-urgent" : t.priority === "high" ? " is-high" : ""),
      draggable: "true", dataset: { id: t.id },
    },
      h("div", { class: "board-card-title" }, t.title),
      h("div", { class: "board-card-meta" },
        t.priority === "urgent" ? h("span", { class: "badge badge-urgent" }, "Urgent")
          : t.priority === "high" ? h("span", { class: "badge badge-high" }, "High") : null,
        t.due_date ? h("span", { class: late ? "text-danger" : null }, (late ? "Overdue · " : "Due ") + formatDate(t.due_date))
          : h("span", {}, "No due date"),
        t.meeting_title ? h("a", { href: "meeting.html?id=" + encodeURIComponent(t.meeting_id) + "#tasks" }, t.meeting_title) : null),
      h("div", { class: "board-card-foot" },
        h("span", { class: "person" }, h("span", { class: "avatar", "aria-hidden": "true" }, M.initials(owner)), h("span", {}, owner)),
        select));
    el.addEventListener("dragstart", (e) => {
      dragging = t;
      el.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", t.id);
    });
    el.addEventListener("dragend", () => { dragging = null; el.classList.remove("is-dragging"); });
    return el;
  }

  function render() {
    replace(board, COLUMNS.map(([status, label]) => {
      let items = tasks.filter((t) => t.status === status);
      const total = items.length;
      let more = null;
      if (status === "done" && !showAllDone && total > DONE_PREVIEW) {
        items = items.slice(0, DONE_PREVIEW);
        more = h("button", { class: "btn btn-ghost btn-sm board-more", type: "button", onclick: () => { showAllDone = true; render(); } },
          `Show all ${total}`);
      }
      return column(label, status, [
        items.length ? items.map(card) : h("p", { class: "muted small", style: "padding:8px 4px" },
          status === "open" ? "Nothing to do here." : status === "in_progress" ? "Drag a task here when work starts." : "Finished tasks land here."),
        more,
      ], total);
    }));
  }

  /* ---------- moving a card ---------- */
  async function move(task, status) {
    const before = task.status;
    task.status = status;
    render(); // optimistic: feels instant
    try {
      const { task: updated } = await api.patch("/tasks/" + task.id, { status });
      Object.assign(task, updated);
      toast(`“${task.title}” moved to ${COLUMNS.find(([s]) => s === status)[1]}.`, "success");
    } catch (err) {
      task.status = before;
      render();
      toast(err.message, "error");
    }
  }

  load();
})();
