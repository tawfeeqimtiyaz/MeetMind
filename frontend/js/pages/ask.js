/* Ask MeetMind: a chat across all meetings and documents (multi-tool agent + RAG).
   Answers have numbered, clickable sources; follow-up questions remember the conversation. */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, api, formatDateTime, icon, toast } = M;
  const $ = (id) => document.getElementById(id);

  // Start the login check, but attach the form handlers right away so an early question isn't lost
  const ready = M.requireUser();

  const log = $("ask-log");
  const form = $("ask-form");
  const input = $("ask-input");
  let asking = false;
  let turn = 0;
  let history = []; // earlier turns, so follow-up questions ("and who owns that?") make sense
  const TOOLS = { search_meeting: "meeting", search_documents: "documents", search_slides: "slides", search_meetings: "meetings", list_tasks: "tasks", list_meetings: "meeting list", get_meeting_details: "meeting notes", rag: "search" };

  const shortModel = (name) => String(name || "").split(/[:/]/).pop();

  function timestamp(seconds) {
    const s = Math.floor(seconds || 0);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  }

  function sourceCard(src, id) {
    const where = src.kind === "notes" ? "Meeting notes" : src.kind === "document" ? `📄 ${src.document_name}, page ${src.page}`
      : src.kind === "slide" ? "🖼 Slide" + (src.start != null ? " shown at " + timestamp(src.start) : "")
      : src.kind === "task" ? "Task" : src.kind === "meeting" ? "Meeting overview"
      : src.start != null ? "at " + timestamp(src.start) : "Transcript";
    // documents are often Markdown: show the words, not the ** and ## symbols
    const plain = src.text.replace(/\*\*|__|`/g, "").replace(/(^|\s)#{1,6}\s+/g, "$1");
    const text = plain.length > 320 ? plain.slice(0, 320).trimEnd() + "…" : plain;
    const tab = src.kind === "document" ? "#documents" : src.kind === "slide" ? "#slides" : src.kind === "task" ? "#tasks" : src.kind === "notes" ? "#notes" : src.start != null ? "#transcript" : "";
    return h("li", { class: "source", id },
      h("a", { class: "source-link", href: "meeting.html?id=" + encodeURIComponent(src.meeting_id) + tab },
        h("span", { class: "source-n" }, String(src.n)),
        h("span", { class: "source-title" }, src.meeting_title),
        h("span", { class: "muted small" }, formatDateTime(src.scheduled_at) + " · " + where),
      ),
      h("p", { class: "source-text" }, text),
    );
  }

  function add(node) {
    $("ask-empty").hidden = true;
    $("new-chat").hidden = false;
    log.append(node);
    log.scrollTop = log.scrollHeight;
    return node;
  }

  function userTurn(q) {
    return h("div", { class: "ask-turn is-user" }, h("div", { class: "msg msg-user" }, q));
  }

  function aiTurn(...content) {
    const bubble = h("div", { class: "msg msg-ai" }, ...content);
    return { el: h("div", { class: "ask-turn" }, h("span", { class: "ai-avatar", "aria-hidden": "true" }, icon("i-sparkles")), bubble), bubble };
  }

  async function copy(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied ✓";
      setTimeout(() => { button.textContent = "Copy"; }, 1500);
    } catch (e) { toast("Couldn’t copy. Select the text and copy it instead.", "error"); }
  }

  async function ask(question) {
    const q = question.trim();
    if (!q || asking) return;
    asking = true;
    $("ask-send").disabled = true;
    turn += 1;
    const t = turn;
    add(userTurn(q));
    const pending = aiTurn(h("span", { class: "typing", "aria-hidden": "true" }, h("span"), h("span"), h("span")),
      h("span", { class: "sr-only" }, "Searching your meetings…"));
    add(pending.el);
    try {
      await ready;
      const { answer, model, sources, tools } = await api.post("/search/ask", { question: q, history: history.slice(-6) });
      history.push({ role: "user", content: q }, { role: "assistant", content: answer });
      const looked = [...new Set((tools || []).map((x) => TOOLS[x] || x))];
      const cite = (n) => (sources.some((s) => s.n === n) ? `#src-${t}-${n}` : null);
      const copyBtn = h("button", { class: "btn btn-ghost", type: "button" }, "Copy");
      copyBtn.addEventListener("click", () => copy(answer, copyBtn));
      const sourcesBox = sources.length ? h("details", { class: "sources-box", open: sources.length <= 3 },
        h("summary", {}, `${sources.length} source${sources.length === 1 ? "" : "s"}`),
        h("ol", { class: "sources" }, sources.map((s) => sourceCard(s, `src-${t}-${s.n}`)))) : null;
      replace(pending.bubble,
        M.markdownLite(answer, { cite }),
        sourcesBox,
        h("div", { class: "msg-actions" }, copyBtn,
          h("span", { class: "msg-meta", style: "margin:0" }, (looked.length ? "Looked in " + looked.join(", ") : "") + (model ? (looked.length ? " · " : "") + "answered by " + shortModel(model) : ""))),
      );
    } catch (err) {
      pending.bubble.classList.add("msg-error");
      replace(pending.bubble, err.message);
    } finally {
      asking = false;
      $("ask-send").disabled = false;
      log.scrollTop = log.scrollHeight;
      input.focus();
    }
  }

  // a citation inside a collapsed "sources" box: open the box first, then jump
  log.addEventListener("click", (e) => {
    const link = e.target.closest("a.cite");
    if (!link) return;
    const target = document.getElementById(link.getAttribute("href").slice(1));
    const box = target && target.closest("details");
    if (box && !box.open) box.open = true;
  });

  /* ---------- composer: Enter sends, Shift+Enter new line, grows with the text ---------- */
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight + 2, 180) + "px";
  }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value;
    input.value = "";
    autosize();
    ask(q);
  });

  $("new-chat").addEventListener("click", () => {
    history = [];
    log.querySelectorAll(".ask-turn").forEach((el) => el.remove());
    $("ask-empty").hidden = false;
    $("new-chat").hidden = true;
    input.focus();
  });

  log.addEventListener("click", (e) => { const chip = e.target.closest("[data-suggest]"); if (chip) ask(chip.textContent); });
  input.focus();

  // suggestions about the latest meetings, so people see what they can ask
  try {
    await ready;
    const { items } = await api.get("/meetings?limit=3&status=completed");
    items.slice(0, 2).forEach((m) => $("suggestions").prepend(
      h("button", { class: "chip", type: "button", "data-suggest": true }, `What was decided in “${m.title}”?`)));
  } catch (e) { /* the fixed suggestions are enough */ }
})();
