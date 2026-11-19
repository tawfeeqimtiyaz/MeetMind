/* Meeting page: results, AI task review, recording upload/processing, transcript, Q&A. */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, icon, badge, empty, loading, formatDateTime, plural, api, toast } = M;
  const $ = (id) => document.getElementById(id);

  const params = new URLSearchParams(location.search);
  const id = params.get("id");
  const justCreated = params.has("created");
  if (justCreated) history.replaceState(null, "", "meeting.html?id=" + encodeURIComponent(id));

  const user = await M.requireUser();
  const isAdmin = user.role === "admin";
  const loadingEl = $("loading");

  if (!id) {
    replace(loadingEl, empty("No meeting selected.", "Open a meeting from your dashboard."));
    return;
  }
  replace(loadingEl, loading("Loading meeting…"));

  let meeting = null;
  let pollTimer = null;
  let lastStatus = null;
  let transcriptLoaded = false;

  const BUSY = new Set(["queued", "processing"]);
  const isBusy = (m) => m.status === "processing" || m.recordings.some((r) => BUSY.has(r.status));
  const shortModel = (name) => String(name || "AI").split(/[:/]/).pop();

  /* ---------- loading & polling ---------- */

  async function load() {
    clearTimeout(pollTimer);
    try {
      meeting = (await api.get("/meetings/" + encodeURIComponent(id))).meeting;
    } catch (err) {
      if (err.status === 404 || err.status === 422) {
        $("meeting").hidden = true;
        replace(loadingEl, empty("Meeting not found.", "It may have been deleted, or you don’t have access to it."));
        return;
      }
      if (!meeting) { M.showPageError(loadingEl, err); return; }
      pollTimer = setTimeout(load, 8000); // keep showing what we have; try again
      return;
    }
    if (lastStatus && lastStatus !== meeting.status) announce(meeting.status);
    lastStatus = meeting.status;
    render();
    if (isBusy(meeting)) pollTimer = setTimeout(load, 4000);
  }

  function announce(status) {
    transcriptLoaded = false;
    if (status === "review") toast(isAdmin ? "AI results are ready for your review." : "The recording has been processed.");
    else if (status === "completed") toast("Meeting notes are ready.");
    else if (status === "failed") toast("Processing failed. See the message at the top.", "error");
  }

  /* ---------- render ---------- */

  function render() {
    loadingEl.replaceChildren();
    $("meeting").hidden = false;
    document.title = meeting.title + " · MeetMind";

    $("title").textContent = meeting.title;
    // Members don't see AI drafts, so "review" just means "not ready yet" for them
    replace($("status"), badge(!isAdmin && meeting.status === "review" ? "processing" : meeting.status));
    replace($("meta"),
      h("span", {}, icon("i-clock"), formatDateTime(meeting.scheduled_at)),
      h("span", {}, icon("i-users"), plural(meeting.participant_count, "participant")),
      meeting.agenda ? h("span", { class: "meta-agenda" }, icon("i-target"), meeting.agenda) : null,
    );
    $("delete-meeting").hidden = !isAdmin;

    renderBanner();
    renderSummary();
    renderBullets("shifts", meeting.strategy_shifts);
    renderBullets("questions", meeting.open_questions);
    renderTasks();
    renderRecordings();
    renderPeople();
    renderTranscriptCard();
    renderChatState();
  }

  function renderBanner() {
    const banner = $("banner");
    const latest = meeting.recordings[meeting.recordings.length - 1];

    if (isBusy(meeting)) {
      replace(banner, h("div", { class: "alert alert-info banner" },
        h("span", { class: "spinner", "aria-hidden": "true" }),
        h("div", {}, h("strong", {}, "Processing the recording… "),
          "Transcription and AI analysis usually take under a minute. This page updates by itself.")));
    } else if (meeting.status === "failed") {
      replace(banner, h("div", { class: "alert alert-error banner" },
        icon("i-info"),
        h("div", {}, h("strong", {}, "Processing failed. "), (latest && latest.error) || "Something went wrong."),
        isAdmin && latest ? h("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: retry }, "Retry processing") : null));
    } else if (meeting.status === "review") {
      const n = meeting.tasks.filter((t) => !t.approved).length;
      replace(banner, h("div", { class: "alert alert-info banner" }, icon("i-shield"),
        isAdmin
          ? h("div", {}, h("strong", {}, `AI drafted ${plural(n, "task")}. `), "Review them below. Nothing is shared with owners until you approve.")
          : h("div", {}, "The AI results are being reviewed by an admin.")));
    } else if (justCreated && meeting.status === "scheduled" && !meeting.recordings.length) {
      replace(banner, h("div", { class: "alert alert-info banner" }, icon("i-info"),
        h("div", {}, h("strong", {}, "Meeting created. "), "After the meeting, upload its recording below.")));
    } else {
      banner.replaceChildren();
    }
  }

  async function retry() {
    try {
      await api.post(`/meetings/${id}/reprocess`);
      toast("Processing restarted.");
      load();
    } catch (err) { toast(err.message, "error"); }
  }

  function renderSummary() {
    if (meeting.summary) {
      replace($("summary"), h("div", { class: "prose" }, h("p", {}, meeting.summary)));
      replace($("ai-model"), h("span", { class: "badge badge-ai", title: "Generated by " + meeting.ai_model }, "AI · " + shortModel(meeting.ai_model)));
    } else {
      $("ai-model").replaceChildren();
      const text = isBusy(meeting) ? "The summary appears here once processing finishes."
        : isAdmin ? "Upload the recording after the meeting to get a summary, decisions and tasks."
        : "The summary appears here after the meeting has been processed.";
      replace($("summary"), h("p", { class: "muted" }, text));
    }
  }

  function renderBullets(name, items) {
    $(name + "-card").hidden = !(items && items.length);
    replace($(name), (items || []).map((x) => h("li", {}, x)));
  }

  /* ---------- tasks ---------- */

  function ownerOptions(select, selectedEmail) {
    const seen = new Set();
    const opts = [h("option", { value: "" }, "Unassigned")];
    for (const p of meeting.participants) {
      seen.add(p.email);
      opts.push(h("option", { value: p.email, selected: p.email === selectedEmail }, p.name ? `${p.name} (${p.email})` : p.email));
    }
    if (selectedEmail && !seen.has(selectedEmail)) opts.push(h("option", { value: selectedEmail, selected: true }, selectedEmail));
    replace(select, opts);
    return select;
  }

  async function patchTask(task, body, control) {
    control.disabled = true;
    try {
      const { task: updated } = await api.patch("/tasks/" + task.id, body);
      Object.assign(task, updated);
    } catch (err) {
      toast(err.message, "error");
      load();
    } finally {
      control.disabled = false;
    }
  }

  function reviewPanel(drafts) {
    const rows = drafts.map((t) => {
      const keep = h("input", { type: "checkbox", id: "keep-" + t.id, checked: true });
      const owner = ownerOptions(h("select", { class: "input input-sm", "aria-label": "Owner for " + t.title }),
        t.assignee ? t.assignee.email : t.assignee_email || "");
      owner.addEventListener("change", () => patchTask(t, { assignee_email: owner.value || null }, owner));
      const due = h("input", { type: "date", class: "input input-sm", value: t.due_date || "", "aria-label": "Due date for " + t.title });
      due.addEventListener("change", () => patchTask(t, { due_date: due.value || null }, due));

      const el = h("div", { class: "draft" },
        keep,
        h("div", { class: "draft-body" },
          h("label", { class: "task-title", for: keep.id }, t.title),
          t.evidence ? h("p", { class: "evidence" }, "“" + t.evidence + "”") : null,
          h("div", { class: "draft-fields" },
            h("label", {}, h("span", { class: "small muted" }, "Owner"), owner),
            h("label", {}, h("span", { class: "small muted" }, "Due"), due),
          ),
          t.suggested_owner && !t.assignee && !t.assignee_email
            ? h("p", { class: "small muted" }, `The AI heard “${t.suggested_owner}” but couldn’t match a participant.`) : null,
        ),
      );
      keep.addEventListener("change", () => { el.classList.toggle("is-discarded", !keep.checked); updateButton(); });
      return { t, keep, el };
    });

    const button = h("button", { class: "btn btn-primary", type: "button" });
    function updateButton() {
      const n = rows.filter((r) => r.keep.checked).length;
      button.textContent = n ? `Approve ${plural(n, "task")}` : "Discard all drafts";
      button.className = "btn " + (n ? "btn-primary" : "btn-danger");
    }
    updateButton();

    button.addEventListener("click", async () => {
      const ids = rows.filter((r) => r.keep.checked).map((r) => r.t.id);
      const discard = rows.length - ids.length;
      if (discard && !confirm(`Discard ${plural(discard, "unticked draft")}? This can’t be undone.`)) return;
      button.disabled = true;
      try {
        await api.post(`/meetings/${id}/review`, { approved_task_ids: ids });
        toast(ids.length ? `Approved ${plural(ids.length, "task")}. Owners with an account have been emailed.` : "Drafts discarded.");
        load();
      } catch (err) {
        button.disabled = false;
        toast(err.message, "error");
      }
    });

    return h("div", { class: "review-panel" }, rows.map((r) => r.el),
      h("div", { class: "review-actions" }, h("span", { class: "small muted" }, "Unticked drafts are discarded."), button));
  }

  async function deleteTask(task) {
    if (!confirm(`Delete the task “${task.title}”?`)) return;
    try {
      await api.del("/tasks/" + task.id);
      toast("Task deleted.");
      load();
    } catch (err) { toast(err.message, "error"); }
  }

  function renderTasks() {
    const drafts = meeting.tasks.filter((t) => !t.approved);
    const approved = meeting.tasks.filter((t) => t.approved);
    const reviewing = isAdmin && meeting.status === "review" && drafts.length > 0;

    $("tasks-subtitle").textContent = reviewing
      ? "Drafted by AI from the transcript. Keep the right ones, fix owners and dates, then approve."
      : approved.length ? (isAdmin ? "Owners are emailed when tasks are approved." : "Tick your tasks when they’re done.") : "";
    replace($("review-panel"), reviewing ? reviewPanel(drafts) : null);

    const list = $("task-list");
    if (approved.length) {
      replace(list, approved.map((t) => M.taskRow(t, {
        canToggle: isAdmin || (t.assignee && t.assignee.id === user.id),
        showOwner: true,
        onDelete: isAdmin ? deleteTask : null,
      })));
    } else {
      replace(list, reviewing ? null : h("p", { class: "muted" }, "No action items yet."));
    }

    $("add-task").hidden = !isAdmin;
    if (isAdmin) ownerOptions($("new-task-owner"), "");
  }

  $("add-task-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    if (!form.reportValidity()) return;
    const data = new FormData(form);
    try {
      await api.post(`/meetings/${id}/tasks`, {
        title: data.get("title"),
        assignee_email: data.get("assignee_email") || null,
        due_date: data.get("due_date") || null,
      });
      form.reset();
      toast("Task added.");
      load();
    } catch (err) { toast(err.message, "error"); }
  });

  /* ---------- recordings ---------- */

  function renderRecordings() {
    $("recording-card").hidden = !isAdmin;
    if (!isAdmin) return;
    replace($("recordings"), meeting.recordings.slice().reverse().map((r) =>
      h("div", { class: "list-item list-item-static" },
        icon("i-file"),
        h("div", {},
          h("div", { class: "title" }, r.original_filename),
          h("div", { class: "meta" },
            h("span", {}, M.formatBytes(r.size_bytes)),
            h("span", {}, formatDateTime(r.created_at)),
            r.error ? h("span", { class: "text-danger" }, r.error) : null,
          ),
        ),
        BUSY.has(r.status)
          ? h("span", { class: "loading-inline" }, h("span", { class: "spinner", "aria-hidden": "true" }), r.status === "queued" ? "Queued" : "Processing")
          : badge(r.status === "done" ? "completed" : r.status),
      )));
    $("upload-form").classList.toggle("is-disabled", isBusy(meeting));
    $("upload-form").querySelector("input[type=file]").disabled = isBusy(meeting);
  }

  $("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const input = form.querySelector("input[type=file]");
    const file = input.files[0];
    if (!file) return;
    if (isBusy(meeting)) { toast("Wait until the current recording has finished processing.", "error"); return; }

    const progress = $("upload-progress");
    const bar = progress.querySelector(".progress-bar span");
    const label = progress.querySelector("[aria-live]");
    const button = form.querySelector("button[type=submit]");
    const body = new FormData();
    body.append("file", file);

    button.disabled = true;
    progress.hidden = false;
    try {
      await api.upload(`/meetings/${id}/recordings`, body, (p) => {
        bar.style.width = Math.round(p * 100) + "%";
        label.textContent = p < 1 ? `Uploading… ${Math.round(p * 100)}%` : "Uploaded. Starting processing…";
      });
      toast("Recording uploaded. Processing has started.");
      form.reset();
      $("file-chip").hidden = true;
      load();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      button.disabled = false;
      progress.hidden = true;
      bar.style.width = "0";
    }
  });

  /* ---------- people ---------- */

  function renderPeople() {
    if (!meeting.participants.length) {
      replace($("people"), h("p", { class: "muted small" }, "No participants added."));
      return;
    }
    replace($("people"), meeting.participants.map((p) =>
      h("div", { class: "person" },
        h("span", { class: "avatar", "aria-hidden": "true" }, M.initials(p.name || p.email)),
        h("div", {},
          h("div", { class: "person-name" }, p.name || p.email),
          p.name ? h("div", { class: "small muted" }, p.email) : null,
          !p.is_user && isAdmin ? h("div", { class: "small muted" }, "No MeetMind account yet") : null,
        ))));
  }

  /* ---------- transcript ---------- */

  function renderTranscriptCard() {
    $("transcript-card").hidden = !meeting.has_transcript;
    if (!meeting.has_transcript) { $("transcript").open = false; transcriptLoaded = false; }
    else if ($("transcript").open && !transcriptLoaded) loadTranscript();
  }

  function timestamp(seconds) {
    const s = Math.floor(seconds || 0);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  }

  async function loadTranscript() {
    transcriptLoaded = true;
    const body = $("transcript-body");
    replace(body, loading("Loading transcript…"));
    try {
      const { transcript } = await api.get(`/meetings/${id}/transcript`);
      const lines = transcript.segments.length
        ? transcript.segments.map((s) => h("p", { class: "transcript-line" },
            h("span", { class: "ts" }, timestamp(s.start)), s.speaker ? h("span", { class: "speaker" }, s.speaker) : null, s.text))
        : [h("p", { class: "transcript-line" }, transcript.text)];
      replace(body, lines);
    } catch (err) {
      transcriptLoaded = false;
      M.showPageError(body, err);
    }
  }
  $("transcript").addEventListener("toggle", () => { if ($("transcript").open && !transcriptLoaded) loadTranscript(); });

  /* ---------- Q&A chat ---------- */

  const chatLog = $("chat-log");
  const chatForm = $("chat-form");
  const chatInput = $("chat-input");
  let asking = false;

  function renderChatState() {
    const ready = meeting.has_transcript || Boolean(meeting.summary);
    chatForm.querySelectorAll("input, button").forEach((el) => { el.disabled = !ready; });
    document.querySelectorAll("[data-suggest]").forEach((el) => { el.disabled = !ready; });
    $("chat-hint").hidden = ready;
  }

  function addMessage(who, ...content) {
    const msg = h("div", { class: "msg msg-" + who }, ...content);
    chatLog.append(msg);
    chatLog.scrollTop = chatLog.scrollHeight;
    return msg;
  }

  async function ask(question) {
    const q = question.trim();
    if (!q || asking) return;
    asking = true;
    addMessage("user", q);
    const pending = addMessage("ai", h("span", { class: "typing", "aria-hidden": "true" }, h("span"), h("span"), h("span")),
      h("span", { class: "sr-only" }, "Thinking…"));
    try {
      const { answer, model } = await api.post(`/meetings/${id}/ask`, { question: q });
      replace(pending, M.markdownLite(answer), h("div", { class: "msg-meta" }, "Answered by " + shortModel(model)));
    } catch (err) {
      pending.classList.add("msg-error");
      replace(pending, err.message);
    } finally {
      asking = false;
      chatLog.scrollTop = chatLog.scrollHeight;
    }
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    ask(chatInput.value);
    chatInput.value = "";
  });
  document.querySelectorAll("[data-suggest]").forEach((chip) => chip.addEventListener("click", () => ask(chip.textContent)));

  /* ---------- delete ---------- */

  $("delete-meeting").addEventListener("click", async () => {
    if (!confirm(`Delete “${meeting.title}”? Its recording, transcript and tasks are removed too. This can’t be undone.`)) return;
    try {
      await api.del("/meetings/" + id);
      location.assign("admin.html");
    } catch (err) { toast(err.message, "error"); }
  });

  load();
})();
