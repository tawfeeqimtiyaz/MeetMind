/* Ask MeetMind: questions across all meetings (RAG), answers with numbered, clickable sources. */
(async function () {
  "use strict";
  const M = window.MeetMind;
  const { h, replace, api, formatDateTime } = M;
  const $ = (id) => document.getElementById(id);

  // Start the login check, but attach the form handlers right away so an early question isn't lost
  const ready = M.requireUser();

  const log = $("ask-log");
  const form = $("ask-form");
  const input = $("ask-input");
  let asking = false;
  let turn = 0;
  const history = []; // earlier turns, so follow-up questions ("and who owns that?") make sense
  const TOOLS = { search_meeting: "meeting", search_documents: "documents", search_meetings: "meetings", list_tasks: "tasks", list_meetings: "meeting list", get_meeting_details: "meeting notes", rag: "search" };

  const shortModel = (name) => String(name || "").split(/[:/]/).pop();

  function timestamp(seconds) {
    const s = Math.floor(seconds || 0);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
  }

  function sourceCard(src, id) {
    const where = src.kind === "notes" ? "Meeting notes" : src.kind === "document" ? `📄 ${src.document_name}, page ${src.page}`
      : src.kind === "task" ? "Task" : src.kind === "meeting" ? "Meeting overview"
      : src.start != null ? "at " + timestamp(src.start) : "Transcript";
    const text = src.text.length > 320 ? src.text.slice(0, 320).trimEnd() + "…" : src.text;
    return h("li", { class: "source", id },
      h("a", { class: "source-link", href: "meeting.html?id=" + encodeURIComponent(src.meeting_id) },
        h("span", { class: "source-n" }, String(src.n)),
        h("span", { class: "source-title" }, src.meeting_title),
        h("span", { class: "muted small" }, formatDateTime(src.scheduled_at) + " · " + where),
      ),
      h("p", { class: "source-text" }, text),
    );
  }

  function add(node) {
    $("ask-empty").hidden = true;
    log.append(node);
    node.scrollIntoView({ block: "end", behavior: "smooth" });
    return node;
  }

  async function ask(question) {
    const q = question.trim();
    if (!q || asking) return;
    asking = true;
    turn += 1;
    const t = turn;
    add(h("div", { class: "msg msg-user" }, q));
    const pending = add(h("div", { class: "msg msg-ai ask-answer" },
      h("span", { class: "typing", "aria-hidden": "true" }, h("span"), h("span"), h("span")),
      h("span", { class: "sr-only" }, "Searching your meetings…")));
    try {
      await ready;
      const { answer, model, sources, tools } = await api.post("/search/ask", { question: q, history: history.slice(-6) });
      history.push({ role: "user", content: q }, { role: "assistant", content: answer });
      const looked = [...new Set((tools || []).map((x) => TOOLS[x] || x))];
      const cite = (n) => (sources.some((s) => s.n === n) ? `#src-${t}-${n}` : null);
      replace(pending,
        M.markdownLite(answer, { cite }),
        h("div", { class: "msg-meta" }, (sources.length ? `${sources.length} source${sources.length === 1 ? "" : "s"} · ` : "") + (looked.length ? "looked in " + looked.join(", ") + " · " : "") + (model ? "answered by " + shortModel(model) : "")),
        sources.length ? h("ol", { class: "sources" }, sources.map((s) => sourceCard(s, `src-${t}-${s.n}`))) : null,
      );
    } catch (err) {
      pending.classList.add("msg-error");
      replace(pending, err.message);
    } finally {
      asking = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    ask(input.value);
    input.value = "";
  });
  document.querySelectorAll("[data-suggest]").forEach((chip) => chip.addEventListener("click", () => ask(chip.textContent)));
  input.focus();
})();
