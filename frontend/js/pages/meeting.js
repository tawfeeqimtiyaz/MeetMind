/* Meeting page: section tabs, live recording (focus screen), notes, results, AI task review,
   task assistant, documents, playback (mini player), Q&A chat (column or drawer). */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, icon, badge, empty, formatDateTime, plural, api, toast } = M;
  const $ = (id) => document.getElementById(id);

  const params = new URLSearchParams(location.search);
  const id = params.get("id");
  const justCreated = params.has("created");
  const wantsRecording = params.has("record");
  if (justCreated || wantsRecording) history.replaceState(null, "", "meeting.html?id=" + encodeURIComponent(id) + location.hash);

  const user = await M.requireUser();
  const isAdmin = user.role === "admin";
  const loadingEl = $("loading");

  if (!id) {
    replace(loadingEl, empty("No meeting selected.", "Open a meeting from your dashboard.", { icon: "i-calendar" }));
    return;
  }
  replace(loadingEl, M.skeleton(4));

  let meeting = null;
  let pollTimer = null;
  let lastStatus = null;
  let transcriptLoaded = false;
  let recorder = null; // MeetingRecorder while recording on this page
  let firstRender = true;

  const BUSY = new Set(["queued", "processing"]);
  const isBusy = (m) => m.status === "processing" || m.recordings.some((r) => BUSY.has(r.status));
  const shortModel = (name) => String(name || "AI").split(/[:/]/).pop();
  const pad = (n) => String(n).padStart(2, "0");
  const timestamp = (sec) => {
    const s = Math.floor(sec || 0);
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  };
  const recording = () => recorder && ["recording", "paused"].includes(recorder.state);

  /* ================= section tabs ================= */

  const TABS = ["overview", "tasks", "notes", "transcript", "documents", "recording"];
  let currentTab = null;

  function visibleTabs() { return TABS.filter((t) => !$("mtab-" + t).hidden); }

  function showTab(name, opts) {
    const o = opts || {};
    if (!visibleTabs().includes(name)) name = "overview";
    currentTab = name;
    for (const t of TABS) {
      const tab = $("mtab-" + t);
      tab.setAttribute("aria-selected", String(t === name));
      tab.tabIndex = t === name ? 0 : -1;
      $("mpanel-" + t).hidden = t !== name;
    }
    if (o.remember) history.replaceState(null, "", location.pathname + location.search + "#" + name);
    if (meeting && !firstRender) renderBanner();
    if (name === "transcript" && meeting && meeting.has_transcript && !transcriptLoaded) loadTranscript();
    if (o.focusPanel) $("mpanel-" + name).focus({ preventScroll: true });
    if (o.scroll) $("meeting-tabs").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  $("meeting-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".mtab");
    if (tab) showTab(tab.dataset.tab, { remember: true });
  });
  $("meeting-tabs").addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const tabs = visibleTabs();
    const focused = e.target.closest(".mtab");
    let i = tabs.indexOf(focused ? focused.dataset.tab : currentTab);
    i = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    e.preventDefault();
    showTab(tabs[i], { remember: true });
    $("mtab-" + tabs[i]).focus();
  });

  function defaultTab() {
    const fromHash = location.hash.slice(1);
    if (TABS.includes(fromHash)) return fromHash;
    if (wantsRecording && isAdmin) return "recording";
    if (isAdmin && meeting.status === "review") return "tasks";
    if (meeting.summary || meeting.has_transcript || isBusy(meeting)) return "overview";
    if (isAdmin) return "recording";
    return "overview";
  }

  function setCount(name, n, alert) {
    const el = $("count-" + name);
    el.hidden = !n;
    el.textContent = n || "";
    el.classList.toggle("is-alert", Boolean(alert));
  }

  /* ================= loading & polling ================= */

  async function load() {
    clearTimeout(pollTimer);
    try {
      meeting = (await api.get("/meetings/" + encodeURIComponent(id))).meeting;
    } catch (err) {
      if (err.status === 404 || err.status === 422) {
        $("meeting").hidden = true;
        replace(loadingEl, empty("Meeting not found.", "It may have been deleted, or you don’t have access to it.", { icon: "i-calendar" }));
        return;
      }
      if (!meeting) { M.showPageError(loadingEl, err); return; }
      pollTimer = setTimeout(load, 8000);
      return;
    }
    if (lastStatus && lastStatus !== meeting.status) announce(meeting.status);
    lastStatus = meeting.status;
    render();
    const docsWaiting = meeting.documents.some((d) => d.status === "queued");
    if (isBusy(meeting) || docsWaiting) pollTimer = setTimeout(load, 3000);
  }

  function announce(status) {
    transcriptLoaded = false;
    if (status === "review") toast(isAdmin ? "AI results are ready for your review." : "The recording has been processed.", "success");
    else if (status === "completed") toast("Meeting notes are ready.", "success");
    else if (status === "failed") toast("Processing failed. See the message at the top.", "error");
  }

  /* ================= render ================= */

  function render() {
    loadingEl.replaceChildren();
    $("meeting").hidden = false;
    $("chat-fab").hidden = false;
    document.title = meeting.title + " · MeetMind";

    $("title").textContent = meeting.title;
    $("rename").hidden = !isAdmin;
    const shown = !isAdmin && meeting.status === "review" ? "processing" : meeting.status;
    replace($("status"), meeting.status === "recording" ? h("span", { class: "badge badge-recording" }, "● Recording") : badge(shown));
    replace($("meta"),
      h("span", {}, icon("i-clock"), formatDateTime(meeting.scheduled_at)),
      h("span", {}, icon("i-users"), plural(meeting.participant_count, "participant")),
      meeting.agenda ? h("span", { class: "meta-agenda" }, icon("i-target"), meeting.agenda) : null,
    );
    $("delete-meeting").hidden = !isAdmin;

    $("mtab-recording").hidden = !isAdmin;
    $("tab-rec-dot").hidden = !meeting.recordings.some((r) => r.status === "recording");
    const drafts = meeting.tasks.filter((t) => !t.approved).length;
    setCount("tasks", meeting.tasks.length, isAdmin && meeting.status === "review" && drafts);
    setCount("notes", meeting.notes.length);
    setCount("documents", meeting.documents.length);
    if (firstRender) showTab(defaultTab());

    renderBanner();
    renderStepper();
    renderSummary();
    renderBullets("shifts", meeting.strategy_shifts);
    renderBullets("questions", meeting.open_questions);
    renderFollowUps();
    renderTasks();
    renderRecordings();
    renderNotes();
    renderDocuments();
    renderPeople();
    renderTranscriptTab();
    renderChatState();
    $("assistant-card").hidden = !isAdmin;

    if (firstRender) {
      firstRender = false;
      if (wantsRecording && isAdmin) $("rec-start").focus();
      if (isAdmin) recoverIfInterrupted();
    }
  }

  function renderBanner() {
    const banner = $("banner");
    const liveElsewhere = meeting.recordings.find((r) => r.status === "recording") && !recorder;
    const latest = meeting.recordings[meeting.recordings.length - 1];

    if (liveElsewhere && isAdmin) {
      const rec = meeting.recordings.find((r) => r.status === "recording");
      replace(banner, h("div", { class: "alert alert-info banner" }, icon("i-mic"),
        h("div", {}, h("strong", {}, "A live recording is open. "),
          `${plural(rec.chunks_received || 0, "piece")} saved so far. If it was interrupted, process what was recorded or discard it.`),
        h("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: () => finishInterrupted(rec) }, "Process it"),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => discardRecording(rec) }, "Discard")));
    } else if (meeting.status === "failed") {
      replace(banner, h("div", { class: "alert alert-error banner" }, icon("i-info"),
        h("div", {}, h("strong", {}, "Processing failed. "), (latest && latest.error) || "Something went wrong."),
        isAdmin && latest ? h("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: retry }, "Retry processing") : null));
    } else if (meeting.status === "review" && !isBusy(meeting)) {
      const n = meeting.tasks.filter((t) => !t.approved).length;
      replace(banner, h("div", { class: "alert alert-info banner" }, icon("i-shield"),
        isAdmin
          ? h("div", {}, h("strong", {}, `AI drafted ${plural(n, "task")}. `), "Review them, or tell the task assistant what to do. Nothing is shared until you approve.")
          : h("div", {}, "The AI results are being reviewed by an admin."),
        isAdmin && currentTab !== "tasks"
          ? h("button", { class: "btn btn-primary btn-sm", type: "button", onclick: () => { showTab("tasks", { remember: true, scroll: true }); renderBanner(); } }, "Review tasks") : null));
    } else if (justCreated && meeting.status === "scheduled" && !meeting.recordings.length) {
      replace(banner, h("div", { class: "alert alert-info banner" }, icon("i-info"),
        h("div", {}, h("strong", {}, "Meeting created. "), "Start recording when the meeting begins, or upload a recording afterwards.")));
    } else {
      banner.replaceChildren();
    }
  }

  async function retry() {
    try { await api.post(`/meetings/${id}/reprocess`); toast("Processing restarted."); load(); }
    catch (err) { toast(err.message, "error"); }
  }

  /* --- processing steps --- */

  const STAGE_TEXT = {
    queued: "Waiting for a free worker…",
    preparing: "Joining the recorded pieces into one file…",
    transcribing: "Turning speech into text…",
    analysing: "AI is writing the summary, decisions, questions & answers and tasks…",
  };

  function renderStepper() {
    const card = $("stepper-card");
    const rec = meeting.recordings.slice().reverse().find((r) => BUSY.has(r.status));
    if (!rec) { card.hidden = true; return; }
    const live = rec.mode && rec.mode !== "upload";
    const steps = [["saved", live ? "Recorded" : "Uploaded"]];
    if (live) steps.push(["preparing", "Preparing audio"]);
    steps.push(["transcribing", "Transcribing"], ["analysing", "Analysing"], ["ready", isAdmin ? "Ready for review" : "Ready"]);
    const stage = rec.status === "queued" ? steps[1][0] : rec.stage || "transcribing";
    const current = Math.max(1, steps.findIndex(([key]) => key === stage));
    replace($("stepper"), steps.map(([, label], i) => h("li", {
      class: "step-item" + (i < current ? " is-done" : i === current ? " is-current" : ""),
      "aria-current": i === current ? "step" : null,
    },
    h("span", { class: "step-dot", "aria-hidden": "true" },
      i < current ? "✓" : i === current ? h("span", { class: "spinner" }) : String(i + 1)),
    h("span", { class: "step-label" }, label))));
    $("stepper-note").textContent = (STAGE_TEXT[rec.status === "queued" ? "queued" : stage] || "") +
      " Usually under a minute, longer for long meetings. This page updates by itself.";
    card.hidden = false;
  }

  function renderSummary() {
    if (meeting.summary) {
      replace($("summary"), h("div", { class: "prose" }, h("p", {}, meeting.summary)));
      replace($("ai-model"), h("span", { class: "badge badge-ai", title: "Generated by " + meeting.ai_model }, "AI · " + shortModel(meeting.ai_model)));
    } else {
      $("ai-model").replaceChildren();
      const busy = isBusy(meeting);
      replace($("summary"), busy
        ? M.skeleton(2)
        : empty("No summary yet.",
          isAdmin ? "Record the meeting or upload a recording to get a summary, decisions, Q&A and tasks."
            : "The summary appears here after the meeting has been processed.",
          isAdmin ? { icon: "i-mic", action: { label: "Go to recording", onclick: () => showTab("recording", { remember: true }) } } : { icon: "i-file" }));
    }
  }

  function renderBullets(name, items) {
    $(name + "-card").hidden = !(items && items.length);
    replace($(name), (items || []).map((x) => h("li", {}, x)));
  }

  function renderFollowUps() {
    const items = meeting.follow_ups || [];
    $("followups-card").hidden = !items.length;
    replace($("followups"), items.map((f) => h("div", { class: "qa" },
      h("p", { class: "qa-q" }, h("span", { class: "qa-tag" }, "Q"), f.question, f.asked_by ? h("span", { class: "muted small" }, " · " + f.asked_by) : null),
      h("p", { class: "qa-a" + (f.answer ? "" : " muted") }, h("span", { class: "qa-tag qa-tag-a" }, "A"),
        f.answer || "Not answered in the meeting", f.answered_by ? h("span", { class: "muted small" }, " · " + f.answered_by) : null),
    )));
  }

  /* ================= tasks ================= */

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

  function prioritySelect(value, label) {
    return h("select", { class: "input input-sm", "aria-label": label },
      ["low", "normal", "high", "urgent"].map((p) => h("option", { value: p, selected: p === (value || "normal") }, p[0].toUpperCase() + p.slice(1))));
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
      const prio = prioritySelect(t.priority, "Priority for " + t.title);
      prio.addEventListener("change", () => patchTask(t, { priority: prio.value }, prio));

      const el = h("div", { class: "draft" },
        keep,
        h("div", { class: "draft-body" },
          h("label", { class: "task-title", for: keep.id }, t.title),
          t.evidence ? h("p", { class: "evidence" }, "“" + t.evidence + "”") : null,
          h("div", { class: "draft-fields" },
            h("label", {}, h("span", { class: "small muted" }, "Owner"), owner),
            h("label", {}, h("span", { class: "small muted" }, "Due"), due),
            h("label", {}, h("span", { class: "small muted" }, "Priority"), prio),
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
      if (discard && !(await M.confirmDialog({
        title: `Discard ${plural(discard, "draft")}?`, body: "Unticked drafts are deleted. This can’t be undone.",
        confirmLabel: "Discard and continue", danger: true,
      }))) return;
      button.disabled = true;
      try {
        await api.post(`/meetings/${id}/review`, { approved_task_ids: ids });
        toast(ids.length ? `Approved ${plural(ids.length, "task")}. Owners with an account have been emailed.` : "Drafts discarded.", "success");
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
    if (!(await M.confirmDialog({ title: "Delete this task?", body: `“${task.title}” will be removed.`, confirmLabel: "Delete", danger: true }))) return;
    try { await api.del("/tasks/" + task.id); toast("Task deleted."); load(); }
    catch (err) { toast(err.message, "error"); }
  }

  function renderTasks() {
    const drafts = meeting.tasks.filter((t) => !t.approved);
    const approved = meeting.tasks.filter((t) => t.approved);
    const reviewing = isAdmin && meeting.status === "review" && drafts.length > 0;

    $("tasks-subtitle").textContent = reviewing
      ? "Drafted by AI from the recording and notes. Keep the right ones, fix owners, dates and priority, then approve."
      : approved.length ? (isAdmin ? "Owners are emailed when tasks are approved, and reminded before deadlines." : "Tick your tasks when they’re done.") : "";
    replace($("review-panel"), reviewing ? reviewPanel(drafts) : null);

    const list = $("task-list");
    if (approved.length) {
      replace(list, approved.map((t) => M.taskRow(t, {
        canToggle: isAdmin || (t.assignee && t.assignee.id === user.id),
        showOwner: true,
        onDelete: isAdmin ? deleteTask : null,
      })));
    } else {
      replace(list, reviewing ? null : empty("No action items yet.",
        isBusy(meeting) ? "Tasks appear here once processing finishes." : isAdmin ? "They’re drafted by AI after a recording, or add one below." : null,
        { icon: "i-tasks" }));
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
        priority: data.get("priority") || "normal",
      });
      form.reset();
      toast("Task added.", "success");
      load();
    } catch (err) { toast(err.message, "error"); }
  });

  /* ================= task assistant ================= */

  const assistantForm = $("assistant-form");
  document.querySelectorAll("[data-instruction]").forEach((chip) => chip.addEventListener("click", () => {
    $("assistant-input").value = chip.textContent;
    $("assistant-input").focus();
  }));

  assistantForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!assistantForm.reportValidity()) return;
    const button = assistantForm.querySelector("button[type=submit]");
    button.disabled = true;
    replace($("assistant-plan"), M.loading("Preparing a plan…"));
    try {
      const { run } = await api.post(`/meetings/${id}/assistant/plan`, { instruction: $("assistant-input").value });
      showPlan(run);
    } catch (err) {
      replace($("assistant-plan"), h("div", { class: "alert alert-error" }, err.message));
    } finally {
      button.disabled = false;
    }
  });

  function showPlan(run) {
    const plan = run.plan || { steps: [], warnings: [] };
    const box = $("assistant-plan");
    const confirmBtn = h("button", { class: "btn btn-primary", type: "button", disabled: !plan.steps.length }, "Confirm");
    const cancelBtn = h("button", { class: "btn btn-ghost", type: "button" }, "Cancel");
    replace(box, h("div", { class: "plan" },
      h("p", { class: "plan-title" }, plan.steps.length ? "Here’s the plan. Nothing has changed yet:" : "I couldn’t turn that into any changes."),
      h("ul", { class: "plan-steps" }, plan.steps.map((s) => h("li", { class: "plan-step plan-" + s.op },
        h("span", { class: "plan-icon", "aria-hidden": "true" }, s.op === "discard" ? "✕" : s.op === "create" ? "+" : "✓"),
        s.preview))),
      plan.notify_owners && plan.steps.some((s) => s.op !== "discard")
        ? h("p", { class: "small" }, "✉ Each owner will be emailed" + (plan.message ? `: “${plan.message}”` : ".")) : null,
      plan.warnings.length ? h("div", { class: "alert alert-warning" }, h("div", {}, plan.warnings.map((w) => h("div", {}, "⚠ " + w)))) : null,
      h("div", { class: "row" }, confirmBtn, cancelBtn, h("span", { class: "small muted" }, "Planned by " + shortModel(run.ai_model))),
    ));
    confirmBtn.addEventListener("click", async () => {
      confirmBtn.disabled = cancelBtn.disabled = true;
      try {
        const res = await api.post(`/agent-runs/${run.id}/confirm`);
        toast(`Done: ${plural(res.run.result.changed, "task")} updated${res.run.result.discarded ? `, ${res.run.result.discarded} discarded` : ""}.`, "success");
        replace(box);
        $("assistant-input").value = "";
        load();
      } catch (err) {
        toast(err.message, "error");
        confirmBtn.disabled = cancelBtn.disabled = false;
      }
    });
    cancelBtn.addEventListener("click", async () => {
      try { await api.post(`/agent-runs/${run.id}/cancel`); } catch (e) { /* already gone */ }
      replace(box);
    });
  }

  /* ================= recording: setup, upload, list ================= */

  function selectTab(live) {
    $("tab-live").setAttribute("aria-selected", String(live));
    $("tab-upload").setAttribute("aria-selected", String(!live));
    $("panel-live").hidden = !live;
    $("panel-upload").hidden = live;
  }
  $("tab-live").addEventListener("click", () => selectTab(true));
  $("tab-upload").addEventListener("click", () => selectTab(false));

  function currentMode() { return (document.querySelector("input[name=mode]:checked") || {}).value || "room"; }
  function updateModeHelp() {
    const mode = currentMode();
    $("share-help").hidden = mode === "room";
    $("noise").closest(".field").hidden = mode !== "room";
  }
  document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", updateModeHelp));
  updateModeHelp();

  async function fillMicrophones() {
    const mics = await M.MeetingRecorder.microphones();
    const select = $("mic");
    const current = select.value;
    replace(select, h("option", { value: "" }, "Default microphone"),
      mics.filter((d) => d.deviceId && d.deviceId !== "default").map((d, i) =>
        h("option", { value: d.deviceId, selected: d.deviceId === current }, d.label || `Microphone ${i + 1}`)));
  }
  if (M.MeetingRecorder.supported()) fillMicrophones();

  function renderRecordings() {
    if (!isAdmin) return;
    const busy = isBusy(meeting) || meeting.recordings.some((r) => r.status === "recording");
    $("rec-start").disabled = busy && !recorder;
    $("upload-form").classList.toggle("is-disabled", busy);
    $("upload-form").querySelector("input[type=file]").disabled = busy;

    replace($("recordings"), meeting.recordings.slice().reverse().map((r) => {
      const playable = r.status === "done" || r.status === "failed";
      return h("div", { class: "list-item list-item-static recording-row" },
        h("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", disabled: !playable,
          "aria-label": "Play " + r.original_filename, title: "Play", onclick: () => play(r) }, "▶"),
        h("div", {},
          h("div", { class: "title" }, r.important ? "★ " : "", r.original_filename),
          h("div", { class: "meta" },
            h("span", {}, { room: "In the room", online: "Online meeting", screen: "Online + screen", upload: "Uploaded" }[r.mode] || r.mode),
            r.duration_seconds ? h("span", {}, timestamp(r.duration_seconds)) : null,
            h("span", {}, M.formatBytes(r.size_bytes)),
            h("span", {}, formatDateTime(r.created_at)),
            r.error ? h("span", { class: "text-danger" }, r.error) : null,
          ),
        ),
        BUSY.has(r.status) || r.status === "recording"
          ? h("span", { class: "loading-inline" }, h("span", { class: "spinner", "aria-hidden": "true" }),
              r.status === "recording" ? "Recording" : r.status === "queued" ? "Queued" : "Processing")
          : badge(r.status === "done" ? "completed" : r.status),
        h("div", { class: "row-actions" },
          h("button", { class: "btn btn-ghost btn-sm" + (r.important ? " is-starred" : ""), type: "button",
            title: r.important ? "Remove star" : "Star as important (highlighted in reminders)",
            onclick: () => star(r) }, r.important ? "★ Starred" : "☆ Star"),
          playable ? h("button", { class: "btn btn-ghost btn-sm text-danger", type: "button", onclick: () => discardRecording(r) }, "Delete") : null,
        ),
      );
    }));
  }

  async function star(r) {
    try { await api.patch(`/recordings/${r.id}`, { important: !r.important }); load(); }
    catch (err) { toast(err.message, "error"); }
  }

  async function discardRecording(r) {
    const live = r.status === "recording";
    const ok = await M.confirmDialog(live
      ? { title: "Discard this unfinished recording?", body: "The pieces saved so far are deleted.", confirmLabel: "Discard", danger: true }
      : { title: "Delete this recording?", body: `“${r.original_filename}” is deleted. The transcript and AI results stay.`, confirmLabel: "Delete", danger: true });
    if (!ok) return;
    try { await api.del(`/recordings/${r.id}`); toast(live ? "Recording discarded." : "Recording deleted."); load(); }
    catch (err) { toast(err.message, "error"); }
  }

  async function finishInterrupted(r) {
    try {
      await api.post(`/recordings/${r.id}/finish`, { chunks: r.chunks_received || 1, duration_seconds: null });
      toast("Processing the recorded part.");
      load();
    } catch (err) {
      toast(err.code === "missing_chunks"
        ? "Some pieces never arrived. MeetMind will process what it has automatically within a few hours."
        : err.message, "error");
    }
  }

  async function recoverIfInterrupted() {
    try {
      const res = await M.recoverInterrupted(id);
      if (res && res.uploaded) { toast(`Recovered ${plural(res.uploaded, "piece")} of an interrupted recording.`); load(); }
    } catch (e) { /* nothing to recover */ }
  }

  /* ================= focus recording screen ================= */

  const waveLevels = new Array(96).fill(0);
  let waveLast = 0;
  let heartbeatLast = 0;
  let focusReturn = null;

  function showFocus(on) {
    const focus = $("focus");
    if (on && focus.hidden) focusReturn = document.activeElement;
    focus.hidden = !on;
    document.body.style.overflow = on ? "hidden" : "";
    $("rec-bar").hidden = on || !recorder;
    document.body.classList.toggle("rec-bar-on", !$("rec-bar").hidden);
    if (on) {
      $("focus-title").textContent = meeting.title;
      renderFocusNotes();
      drawWave();
      setTimeout(() => $("rec-pause").focus(), 0);
    } else if (focusReturn && document.contains(focusReturn) && !focusReturn.closest("#focus")) {
      focusReturn.focus();
    }
  }

  function pushLevel(level, paused) {
    const now = performance.now();
    if (now - waveLast < 80) return;
    waveLast = now;
    waveLevels.push(paused ? 0 : level);
    waveLevels.shift();
    drawWave();
  }

  function drawWave() {
    const canvas = $("wave");
    if ($("focus").hidden) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const hgt = canvas.clientHeight;
    if (!w || !hgt) return;
    if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);
    const styles = getComputedStyle(document.documentElement);
    const paused = recorder && recorder.state === "paused";
    ctx.fillStyle = (paused ? styles.getPropertyValue("--text-subtle") : styles.getPropertyValue("--danger")).trim() || "#dc2626";
    const n = waveLevels.length;
    const bw = w / n;
    waveLevels.forEach((v, i) => {
      const bh = Math.max(3, Math.min(1, v) * hgt * 0.95);
      ctx.globalAlpha = 0.25 + 0.75 * (i / n); // older bars fade out
      const x = i * bw + 1;
      const y = (hgt - bh) / 2;
      if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x, y, Math.max(1, bw - 2), bh, 2); ctx.fill(); }
      else ctx.fillRect(x, y, Math.max(1, bw - 2), bh);
    });
    ctx.globalAlpha = 1;
  }
  window.addEventListener("resize", drawWave);
  document.addEventListener("meetmind:theme", drawWave);

  function renderFocusNotes() {
    const latest = (meeting.notes || []).slice(-3);
    replace($("focus-notes"), latest.length ? latest.map(noteItem)
      : h("li", { class: "muted small" }, "Notes you add appear here and are given to the AI with the recording."));
  }

  $("focus-min").addEventListener("click", () => showFocus(false));
  $("open-focus").addEventListener("click", () => showFocus(true));
  $("rec-bar-open").addEventListener("click", () => showFocus(true));
  $("rec-bar-stop").addEventListener("click", () => stopRecording());

  document.addEventListener("keydown", (e) => {
    if (!recorder || $("focus").hidden || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable]")) {
      if (e.key === "Escape") e.target.blur();
      return;
    }
    if (e.key === " " || e.code === "Space") {
      if (e.target.closest("button")) return; // the focused button handles Space itself
      e.preventDefault();
      togglePause();
    } else if (e.key === "m" || e.key === "M") {
      e.preventDefault();
      markMoment();
    } else if (e.key === "n" || e.key === "N") {
      e.preventDefault();
      $("focus-note").focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      showFocus(false);
    }
  });

  $("focus-note-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("focus-note");
    const text = input.value.trim();
    if (!text) return;
    try {
      await addNote(text);
      input.value = "";
      toast("Note saved at " + timestamp(recorder ? recorder.elapsed() : 0));
    } catch (err) { toast(err.message, "error"); }
  });

  /* --- live recording controls --- */

  function setRecordingUi(on) {
    $("live-setup").hidden = on;
    $("live-running").hidden = !on;
    $("tab-upload").disabled = on;
    M.recordingHere = on;
    if (!on) M.liveClear();
    showFocus(on);
  }

  function togglePause() {
    if (!recorder) return;
    if (recorder.state === "paused") recorder.resume(); else recorder.pause();
  }

  async function markMoment() {
    if (!recorder) return;
    const at = recorder.elapsed();
    try {
      await api.post(`/meetings/${id}/notes`, { text: "Important moment", kind: "bookmark", offset_seconds: Math.round(at) });
      toast("⭐ Marked at " + timestamp(at));
      refreshNotes();
    } catch (err) { toast(err.message, "error"); }
  }

  async function stopRecording() {
    if (!recorder) return;
    if (!(await M.confirmDialog({ title: "Stop recording?", body: "The recording is saved and the AI starts processing the meeting.", confirmLabel: "Stop & process" }))) return;
    $("upload-status").textContent = "Finishing upload…";
    try {
      await recorder.stop();
      recorder = null;
      setRecordingUi(false);
      toast("Recording saved. Processing has started.", "success");
      await load();
      showTab("overview", { remember: true });
    } catch (err) {
      toast(err.message, "error");
    }
  }

  $("rec-start").addEventListener("click", async () => {
    if (!M.MeetingRecorder.supported()) { toast("This browser can’t record. Please use Chrome or Edge.", "error"); return; }
    const mode = currentMode();
    if (mode !== "room" && !M.MeetingRecorder.canCaptureOtherApps()) {
      toast("Capturing another app’s sound needs Chrome or Edge on a computer.", "error"); return;
    }
    $("rec-start").disabled = true;
    recorder = new M.MeetingRecorder(id, {
      time: ({ seconds }) => {
        const t = timestamp(seconds);
        $("rec-timer").textContent = t;
        $("rec-bar-time").textContent = t;
        updateNoteWhen();
        const now = Date.now();
        if (now - heartbeatLast > 4000) { heartbeatLast = now; M.liveHeartbeat(id, meeting.title); }
      },
      level: ({ level, tooQuiet, state }) => {
        pushLevel(level, state === "paused");
        const hint = $("level-hint");
        if (state === "paused") { hint.textContent = "Paused"; hint.className = "small muted"; }
        else if (tooQuiet) { hint.textContent = "Very quiet: move the laptop closer to the speakers or use an external microphone."; hint.className = "small text-warning"; }
        else { hint.textContent = level > 0.08 ? "Good sound level" : "Listening…"; hint.className = "small muted"; }
      },
      state: ({ state }) => {
        const label = state === "paused" ? "PAUSED" : state === "finishing" ? "SAVING" : "REC";
        $("rec-state").textContent = label;
        $("rec-bar-state").textContent = state === "paused" ? "Paused" : state === "finishing" ? "Saving" : "Recording";
        $("rec-badge").classList.toggle("is-paused", state !== "recording");
        $("rec-pause").textContent = state === "paused" ? "Resume" : "Pause";
        $("rec-pause").disabled = $("rec-mark").disabled = state === "finishing";
        $("rec-stop").disabled = $("rec-bar-stop").disabled = state === "finishing";
        drawWave();
      },
      upload: ({ waiting, ok, error }) => {
        $("upload-status").textContent = error ? "Upload problem: " + error
          : ok ? (waiting ? `Saving… ${plural(waiting, "piece")} waiting` : "All audio saved so far ✓")
          : `Offline: ${plural(waiting, "piece")} waiting, will retry automatically. Keep this page open.`;
      },
    });
    try {
      await recorder.start({ mode, deviceId: $("mic").value || null, noiseReduction: $("noise").checked });
      M.liveHeartbeat(id, meeting.title);
      setRecordingUi(true);
      toast("Recording started.");
      fillMicrophones();
      load();
    } catch (err) {
      recorder = null;
      $("rec-start").disabled = false;
      const msg = err && err.name === "NotAllowedError"
        ? "Permission was denied. Allow microphone (and screen) access in the browser’s address bar, then try again."
        : (err && err.message) || "Could not start recording.";
      toast(msg, "error");
    }
  });

  $("rec-pause").addEventListener("click", togglePause);
  $("rec-mark").addEventListener("click", markMoment);
  $("rec-stop").addEventListener("click", stopRecording);
  window.addEventListener("pagehide", () => { if (recorder) M.liveClear(); });

  /* --- upload a file --- */

  $("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const file = form.querySelector("input[type=file]").files[0];
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
      toast("Recording uploaded. Processing has started.", "success");
      form.reset();
      $("file-chip").hidden = true;
      await load();
      showTab("overview", { remember: true });
    } catch (err) {
      toast(err.message, "error");
    } finally {
      button.disabled = false;
      progress.hidden = true;
      bar.style.width = "0";
    }
  });

  /* ================= playback: mini player at the bottom ================= */

  let mediaEl = null;
  let mediaFor = null;

  function closePlayer() {
    if (mediaEl) mediaEl.pause();
    mediaEl = null;
    mediaFor = null;
    $("player").hidden = true;
    $("player").replaceChildren();
    document.body.classList.remove("has-mini-player");
  }

  async function play(r, at) {
    try {
      if (mediaFor !== r.id) {
        const { url, content_type } = await api.get(`/recordings/${r.id}/playback`);
        const video = (content_type || "").startsWith("video");
        mediaEl = h(video ? "video" : "audio", { controls: true, preload: "metadata", src: url, class: "media" });
        mediaFor = r.id;
        replace($("player"),
          h("span", { class: "now", title: r.original_filename }, "▶ " + r.original_filename),
          mediaEl,
          h("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "Close player", title: "Close player", onclick: closePlayer }, icon("i-close")));
        $("player").hidden = false;
        document.body.classList.add("has-mini-player");
      }
      if (at != null) {
        const seek = () => { mediaEl.currentTime = at; mediaEl.play().catch(() => {}); };
        if (mediaEl.readyState >= 1) seek(); else mediaEl.addEventListener("loadedmetadata", seek, { once: true });
      } else {
        mediaEl.play().catch(() => {});
      }
    } catch (err) { toast(err.message, "error"); }
  }

  /* jump to a moment of the meeting's main recording (transcript lines, notes and chat sources) */
  function seekTo(seconds) {
    const rec = meeting.recordings.slice().reverse().find((r) => r.status === "done");
    if (!rec) { toast("The recording isn’t available to play yet.", "error"); return; }
    play(rec, seconds);
  }

  /* ================= notes ================= */

  function updateNoteWhen() {
    $("note-when").textContent = recording()
      ? "Saved at " + timestamp(recorder.elapsed()) + " of the recording · Ctrl + Enter to add" : "Ctrl + Enter to add";
  }

  function noteItem(n) {
    const mine = n.author && n.author.id === user.id;
    return h("li", { class: "note" + (n.kind === "bookmark" ? " note-bookmark" : "") },
      n.offset_seconds != null
        ? h("button", { class: "note-time", type: "button", title: "Play from here", onclick: () => seekTo(n.offset_seconds) }, timestamp(n.offset_seconds))
        : h("span", { class: "note-time is-static" }, new Date(n.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
      h("div", { class: "note-body" },
        h("div", {}, n.kind === "bookmark" ? "⭐ " : "", n.text),
        h("div", { class: "small muted" }, n.author ? (n.author.name || n.author.email) : "Someone")),
      mine || isAdmin ? h("button", { class: "btn btn-ghost btn-icon btn-sm", type: "button", "aria-label": "Delete note",
        title: "Delete note", onclick: () => deleteNote(n) }, icon("i-close")) : null,
    );
  }

  function renderNotes() {
    const notes = meeting.notes || [];
    replace($("notes"), notes.length ? notes.map(noteItem)
      : h("li", {}, empty("No notes yet.", "Anyone in the meeting can add notes, during or after it.", { icon: "i-file" })));
    if (!$("focus").hidden) renderFocusNotes();
  }

  async function refreshNotes() {
    try {
      meeting.notes = (await api.get(`/meetings/${id}/notes`)).items;
      setCount("notes", meeting.notes.length);
      renderNotes();
    } catch (e) { /* next load */ }
  }

  async function addNote(text) {
    const body = { text };
    if (recording()) body.offset_seconds = Math.round(recorder.elapsed());
    await api.post(`/meetings/${id}/notes`, body);
    await refreshNotes();
  }

  $("note-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("note-text").value.trim();
    if (!text) return;
    try {
      await addNote(text);
      $("note-text").value = "";
    } catch (err) { toast(err.message, "error"); }
  });
  $("note-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("note-form").requestSubmit();
  });

  async function deleteNote(n) {
    try { await api.del(`/notes/${n.id}`); refreshNotes(); } catch (err) { toast(err.message, "error"); }
  }

  /* ================= documents ================= */

  function renderDocuments() {
    const docs = meeting.documents || [];
    replace($("documents"), docs.length ? docs.map((d) => h("div", { class: "list-item list-item-static" },
      icon("i-file"),
      h("div", {},
        h("div", { class: "title" }, d.filename),
        h("div", { class: "meta" },
          h("span", {}, M.formatBytes(d.size_bytes)),
          d.pages ? h("span", {}, plural(d.pages, "page")) : null,
          d.uploaded_by ? h("span", {}, d.uploaded_by) : null,
          d.error ? h("span", { class: "text-danger" }, d.error) : null),
      ),
      d.status === "queued" ? h("span", { class: "loading-inline" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Reading")
        : d.status === "ready" ? h("span", { class: "badge badge-completed" }, "Ready") : badge("failed"),
      h("div", { class: "row-actions" },
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => download(d) }, "Download"),
        isAdmin ? h("button", { class: "btn btn-ghost btn-sm text-danger", type: "button", onclick: () => deleteDoc(d) }, "Delete") : null),
    )) : empty("No documents attached.", "Attach specs, documentation or bug reports, then ask the assistant about them.",
      { icon: "i-file", action: { label: "Attach a document", onclick: () => $("doc-file").click() } }));
  }

  $("doc-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    try {
      await api.upload(`/meetings/${id}/documents`, body);
      toast(`“${file.name}” attached. Reading it now…`);
      load();
    } catch (err) { toast(err.message, "error"); }
  });

  async function download(d) {
    try {
      const res = await fetch(`/api/documents/${d.id}/download`, { headers: { Authorization: "Bearer " + M.token() } });
      if (!res.ok) throw new Error("Download failed");
      const url = URL.createObjectURL(await res.blob());
      const a = h("a", { href: url, download: d.filename });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (err) { toast(err.message, "error"); }
  }

  async function deleteDoc(d) {
    if (!(await M.confirmDialog({ title: "Delete this document?", body: `“${d.filename}” is removed from the meeting and from search.`, confirmLabel: "Delete", danger: true }))) return;
    try { await api.del(`/documents/${d.id}`); load(); } catch (err) { toast(err.message, "error"); }
  }

  /* ================= people ================= */

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

  /* ================= transcript ================= */

  function renderTranscriptTab() {
    if (!meeting.has_transcript) {
      transcriptLoaded = false;
      $("transcript-filter").hidden = true;
      replace($("transcript-body"), isBusy(meeting) ? M.skeleton(4)
        : empty("No transcript yet.", "It appears here after a recording has been processed.", { icon: "i-mic" }));
    } else if (currentTab === "transcript" && !transcriptLoaded) {
      loadTranscript();
    }
  }

  async function loadTranscript() {
    transcriptLoaded = true;
    const body = $("transcript-body");
    replace(body, M.skeleton(5));
    try {
      const { transcript } = await api.get(`/meetings/${id}/transcript`);
      const lines = transcript.segments.length
        ? transcript.segments.map((s) => h("p", { class: "transcript-line" },
            h("button", { class: "ts", type: "button", title: "Play from here", onclick: () => seekTo(s.start) }, timestamp(s.start)),
            s.speaker ? h("span", { class: "speaker" }, s.speaker) : null, s.text))
        : [h("p", { class: "transcript-line" }, transcript.text)];
      replace(body, lines, h("p", { class: "muted small", id: "transcript-nomatch", hidden: true }, "No lines match your search."));
      $("transcript-filter").hidden = false;
      filterTranscript();
    } catch (err) {
      transcriptLoaded = false;
      M.showPageError(body, err);
    }
  }

  function filterTranscript() {
    const q = $("transcript-filter").value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll("#transcript-body .transcript-line").forEach((line) => {
      const hit = !q || line.textContent.toLowerCase().includes(q);
      line.hidden = !hit;
      shown += hit;
    });
    const none = $("transcript-nomatch");
    if (none) none.hidden = shown > 0;
  }
  $("transcript-filter").addEventListener("input", filterTranscript);

  /* ================= chat: column on wide screens, drawer on smaller ones ================= */

  function setChatOpen(open) {
    document.body.classList.toggle("chat-open", open);
    $("chat-fab").setAttribute("aria-expanded", String(open));
    if (open) setTimeout(() => $("chat-input").focus(), 50);
    else if (document.activeElement && document.activeElement.closest("#side-column")) $("chat-fab").focus();
  }
  $("chat-fab").addEventListener("click", () => setChatOpen(true));
  $("chat-close").addEventListener("click", () => setChatOpen(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("chat-open")) setChatOpen(false); });
  document.addEventListener("click", (e) => {
    if (document.body.classList.contains("chat-open") && !e.target.closest("#side-column, #chat-fab, .dialog, .toast-region, #player")) setChatOpen(false);
  });

  const chatLog = $("chat-log");
  const chatForm = $("chat-form");
  const chatInput = $("chat-input");
  const chatHistory = [];
  let asking = false;
  let turn = 0;
  const TOOL_LABELS = {
    search_meeting: "this meeting", search_documents: "documents", search_meetings: "other meetings",
    list_tasks: "tasks", list_meetings: "meeting list", get_meeting_details: "meeting notes", rag: "search",
  };

  function renderChatState() {
    const ready = meeting.has_transcript || Boolean(meeting.summary) || meeting.notes.length > 0
      || meeting.documents.some((d) => d.status === "ready");
    chatForm.querySelectorAll("input, button").forEach((el) => { el.disabled = !ready; });
    document.querySelectorAll("[data-suggest]").forEach((el) => { el.disabled = !ready; });
    $("chat-hint").hidden = ready;
  }

  function sourceItem(s, anchor) {
    let where = s.meeting_title || "";
    if (s.kind === "document") where = `📄 ${s.document_name}, page ${s.page}`;
    else if (s.kind === "task") where = "Task · " + (s.meeting_title || "");
    else if (s.kind === "notes") where = "Notes · " + where;
    return h("li", { class: "chat-source", id: anchor },
      h("span", { class: "source-n" }, String(s.n)),
      h("span", { class: "small" }, where),
      s.start != null && s.meeting_id === id
        ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => seekTo(s.start) }, "▶ " + timestamp(s.start)) : null);
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
    turn += 1;
    const t = turn;
    addMessage("user", q);
    const pending = addMessage("ai", h("span", { class: "typing", "aria-hidden": "true" }, h("span"), h("span"), h("span")),
      h("span", { class: "sr-only" }, "Thinking…"));
    try {
      const res = await api.post(`/meetings/${id}/ask`, { question: q, history: chatHistory.slice(-6) });
      const sources = res.sources || [];
      const cite = (n) => (sources.some((s) => s.n === n) ? `#c${t}-${n}` : null);
      const looked = [...new Set((res.tools || []).map((x) => TOOL_LABELS[x] || x))];
      replace(pending,
        M.markdownLite(res.answer, { cite }),
        sources.length ? h("ol", { class: "chat-sources" }, sources.map((s) => sourceItem(s, `c${t}-${s.n}`))) : null,
        h("div", { class: "msg-meta" }, (looked.length ? "Looked in: " + looked.join(", ") + " · " : "") + "answered by " + shortModel(res.model)));
      chatHistory.push({ role: "user", content: q }, { role: "assistant", content: res.answer });
    } catch (err) {
      pending.classList.add("msg-error");
      replace(pending, err.message);
    } finally {
      asking = false;
      chatLog.scrollTop = chatLog.scrollHeight;
    }
  }

  chatForm.addEventListener("submit", (e) => { e.preventDefault(); ask(chatInput.value); chatInput.value = ""; });
  document.querySelectorAll("[data-suggest]").forEach((chip) => chip.addEventListener("click", () => ask(chip.textContent)));

  /* ================= rename & delete ================= */

  $("rename").addEventListener("click", async () => {
    const title = await M.dialog({ title: "Rename meeting", input: { label: "Meeting title", value: meeting.title, maxlength: 200 }, confirmLabel: "Save" });
    if (!title || title === meeting.title) return;
    try { await api.patch("/meetings/" + id, { title }); toast("Meeting renamed.", "success"); load(); } catch (err) { toast(err.message, "error"); }
  });

  $("delete-meeting").addEventListener("click", async () => {
    if (!(await M.confirmDialog({
      title: `Delete “${meeting.title}”?`,
      body: "Its recordings, transcript, notes, documents and tasks are removed too. This can’t be undone.",
      confirmLabel: "Delete meeting", danger: true,
    }))) return;
    try { await api.del("/meetings/" + id); location.assign("admin.html"); } catch (err) { toast(err.message, "error"); }
  });

  load();
})();
