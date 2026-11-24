/* Task rows shared by "My tasks" and the meeting page. */
(function () {
  "use strict";
  const M = window.MeetMind;
  const { h, badge, formatDate, isOverdue, personName, api, toast } = M;

  /**
   * taskRow(task, {canToggle, showMeeting, showOwner, onChange, onDelete})
   * The checkbox marks a task done/open (PATCH /tasks/{id}).
   */
  function taskRow(task, opts) {
    const o = opts || {};
    const id = "task-" + task.id;
    const done = task.status === "done";
    const row = h("div", { class: "task" + (done ? " is-done" : "") });

    const checkbox = h("input", {
      type: "checkbox", id, checked: done, disabled: !o.canToggle,
      "aria-label": (done ? "Mark as not done: " : "Mark done: ") + task.title,
    });
    checkbox.addEventListener("change", async () => {
      const status = checkbox.checked ? "done" : "open";
      checkbox.disabled = true;
      try {
        const { task: updated } = await api.patch("/tasks/" + task.id, { status });
        row.replaceWith(taskRow(updated, o));
        if (o.onChange) o.onChange(updated);
      } catch (err) {
        checkbox.checked = !checkbox.checked;
        checkbox.disabled = false;
        toast(err.message, "error");
      }
    });

    const due = task.due_date
      ? h("span", { class: isOverdue(task.due_date) && !done ? "text-danger" : null },
          (isOverdue(task.due_date) && !done ? "Overdue · " : "Due ") + formatDate(task.due_date))
      : h("span", {}, "No due date");

    row.append(
      checkbox,
      h("div", {},
        h("label", { class: "task-title", for: id }, task.title),
        h("div", { class: "task-meta" },
          o.showMeeting && task.meeting_title
            ? h("a", { href: "meeting.html?id=" + encodeURIComponent(task.meeting_id) }, task.meeting_title) : null,
          o.showOwner ? h("span", {}, personName(task.assignee) ) : null,
          due,
          task.priority === "urgent" ? h("span", { class: "badge badge-urgent" }, "Urgent")
            : task.priority === "high" ? h("span", { class: "badge badge-high" }, "High") : null,
          task.source === "ai" ? h("span", { class: "badge badge-ai" }, "AI") : null,
        ),
        task.evidence ? h("p", { class: "evidence" }, "“" + task.evidence + "”") : null,
      ),
      h("div", { class: "task-actions" },
        badge(task.status),
        o.onDelete ? h("button", {
          class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "Delete task: " + task.title,
          title: "Delete task", onclick: () => o.onDelete(task),
        }, M.icon("i-close")) : null,
      ),
    );
    return row;
  }

  M.taskRow = taskRow;
})();
