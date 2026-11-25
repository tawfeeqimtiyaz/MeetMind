"""Measure MeetMind's AI quality on the golden set ("an exam with an answer key").

    docker compose exec worker python -m evals.run                  # scorecard in the terminal
    docker compose exec worker python -m evals.run --langsmith      # also an experiment in LangSmith
    docker compose exec worker python -m evals.run --suite meetings --only launch-sync

It runs the REAL code (LangGraph pipeline, AI models with fallback, RAG with real embeddings, the agent)
on a throwaway in-memory database. Only speech-to-text is skipped: golden transcripts are used, so the scores
measure the AI's understanding, not the microphone. Exit code 1 if a score is below evals/thresholds.json.
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .scoring import METRICS, failures, judge_grounded, metrics, score_answer, score_meeting

HERE = Path(__file__).parent
DATA = HERE / "data"
RESULTS = HERE / "results"
MEETING_METRICS = [m for m in METRICS if m not in {"answer_correct", "source_hit", "says_when_unknown", "grounded"}]
QUESTION_METRICS = ["answer_correct", "source_hit", "says_when_unknown", "grounded"]

log = logging.getLogger("evals")


# ---------- a throwaway MeetMind workspace ----------

class Workspace:
    def __init__(self):
        from meetmind import notifications
        from meetmind.config import Settings
        from meetmind.db import Base, make_engine, make_session_factory
        from meetmind.models import Organization, User

        self.tmp = tempfile.mkdtemp(prefix="meetmind-eval-")
        self.settings = Settings(
            database_url="sqlite+pysqlite:///:memory:", upload_dir=self.tmp, email_enabled=False,
            ratelimit_enabled=False,
            secret_key=os.environ.get("SECRET_KEY") or "evaluation-only-secret-key-long-enough-for-hs256",
        )
        notifications.deliver = lambda *args, **kwargs: None  # evals never email anyone
        engine = make_engine(self.settings.database_url)
        Base.metadata.create_all(engine)
        self.sf = make_session_factory(engine)
        self.cases = json.loads((DATA / "meetings.json").read_text(encoding="utf-8"))
        self.meeting_ids: dict[str, str] = {}
        with self.sf() as db:
            org = Organization(name="Eval Co")
            admin = User(organization=org, email="admin@acme.test", name="Eval Admin", role="admin", status="active")
            db.add_all([org, admin])
            people = {e: n for case in self.cases for n, e in case["participants"]}
            for email, name in people.items():
                db.add(User(organization=org, email=email, name=name, role="member", status="active"))
            db.commit()
            self.org_id, self.admin_id = org.id, admin.id

    def models(self) -> dict:
        from meetmind.ai.llm import analysis_providers, chat_providers

        s = self.settings
        return {"analysis": [p.name for p in analysis_providers(s)], "chat": [p.name for p in chat_providers(s)],
                "embeddings": s.nvidia_embedding_model if s.embedding_provider == "nvidia" else s.ollama_embedding_model}

    def process(self, case: dict) -> dict:
        """Create the meeting and run it through the real pipeline (golden transcript instead of Whisper)."""
        from meetmind.ai.analysis import analyze_meeting
        from meetmind.ai.pipeline import PipelineDeps, run_pipeline
        from meetmind.ai.transcription import TranscriptionResult
        from meetmind.models import Meeting, MeetingNote, MeetingParticipant, Recording, Slide, User

        with self.sf() as db:
            users = {u.email: u for u in db.query(User).filter_by(organization_id=self.org_id)}
            by_name = {u.name: u for u in users.values()}
            when = datetime.fromisoformat(case["date"]).replace(tzinfo=ZoneInfo(case["timezone"]))
            m = Meeting(organization_id=self.org_id, created_by_id=self.admin_id, title=case["title"],
                        scheduled_at=when.astimezone(timezone.utc), timezone=case["timezone"])
            for _, email in case["participants"]:
                m.participants.append(MeetingParticipant(email=email, user=users.get(email)))
            for name, text in case.get("notes", []):
                m.notes.append(MeetingNote(author=by_name.get(name), text=text, kind="note"))
            for at, title, text in case.get("slides", []):
                m.slides.append(Slide(source="screen", offset_seconds=at, storage_path="eval/none.jpg",
                                      status="ready", title=title, text=text))
            rec = Recording(meeting=m, original_filename="golden.wav", storage_path=f"{uuid.uuid4()}.wav",
                            size_bytes=1, status="queued", mode="upload")
            db.add(m)
            db.commit()
            (Path(self.tmp) / rec.storage_path).write_bytes(b"\0")
            meeting_id, rec_id = str(m.id), str(rec.id)
        self.meeting_ids[case["id"]] = meeting_id

        segments = [{"start": float(t), "end": float(t) + 4, "text": text} for t, text in case["transcript"]]
        transcript = TranscriptionResult(text=" ".join(s["text"] for s in segments), segments=segments,
                                         language="en", provider="golden-transcript")
        deps = PipelineDeps(self.sf, self.settings, lambda path, hint: transcript,
                            lambda ctx, text: analyze_meeting(self.settings, ctx, text))
        started = time.monotonic()
        error = None
        try:
            run_pipeline(deps, rec_id)
        except Exception as exc:  # noqa: BLE001 - a failed case scores zero, the run continues
            error = f"{type(exc).__name__}: {exc}"
        with self.sf() as db:
            m = db.get(Meeting, uuid.UUID(meeting_id))
            return {
                "seconds": round(time.monotonic() - started, 1), "error": error, "model": m.ai_model,
                "summary": m.summary or "", "decisions": m.strategy_shifts or [], "follow_ups": m.follow_ups or [],
                "open_questions": m.open_questions or [],
                "tasks": [{"title": t.title, "owner": t.assignee_email, "priority": t.priority,
                           "due": t.due_date.isoformat() if t.due_date else None} for t in m.tasks],
            }

    def build_search(self):
        """Approve the drafted tasks, attach the bug report, index everything with the real embedding model."""
        from meetmind.ai.rag import MemoryStore, RAGService, make_embedder
        from meetmind.documents import extract_pages
        from meetmind.models import Document, Meeting, Task

        report = DATA / "bug-report.md"
        with self.sf() as db:
            for t in db.query(Task):
                t.approved = True
            incident = db.get(Meeting, uuid.UUID(self.meeting_ids["payment-incident"]))
            doc = Document(meeting=incident, filename=report.name, storage_path="eval/bug-report.md",
                           size_bytes=report.stat().st_size, status="ready", pages=extract_pages(report))
            db.add(doc)
            db.commit()
            self.rag = RAGService(self.settings, MemoryStore(), make_embedder(self.settings))
            try:
                for m in db.query(Meeting):
                    self.rag.index_meeting(m)
                self.rag.index_document(incident, doc)
            except Exception as exc:  # noqa: BLE001 - the agent then falls back to text search
                log.warning("Indexing failed (%s); answers will use text search only", exc)

    def ask(self, question: str) -> dict:
        from meetmind.ai.agents import run_assistant, scope_for
        from meetmind.models import User

        with self.sf() as db:
            scope = scope_for(self.sf, db, self.settings, self.rag, db.get(User, self.admin_id), None)
        started = time.monotonic()
        try:
            res = run_assistant(scope, question)
            out = {"answer": res.answer, "model": res.model, "sources": res.sources, "tools": res.tools_used, "error": None}
        except Exception as exc:  # noqa: BLE001
            out = {"answer": "", "model": None, "sources": [], "tools": [], "error": f"{type(exc).__name__}: {exc}"}
        out["seconds"] = round(time.monotonic() - started, 1)
        return out


# ---------- one case ----------

def run_meeting_case(ws: Workspace, case: dict) -> dict:
    out = ws.process(case)
    counters, problems = score_meeting(case["expected"], out)
    if out["error"]:
        problems.insert(0, "pipeline failed: " + out["error"])
    return {"id": case["id"], "about": case["about"], "output": out, "counters": counters, "problems": problems,
            "scores": {k: v for k, v in metrics(counters).items() if k in MEETING_METRICS}}


def run_question_case(ws: Workspace, case: dict) -> dict:
    out = ws.ask(case["question"])
    counters, problems = score_answer(case, out["answer"], out["sources"])
    if out["error"]:
        problems.insert(0, "assistant failed: " + out["error"])
    if not case.get("unanswerable") and out["sources"]:
        try:
            verdict = judge_grounded(ws.settings, case["question"], out["answer"], out["sources"])
        except Exception as exc:  # noqa: BLE001
            verdict = None
            problems.append(f"judge failed: {type(exc).__name__}")
        if verdict is not None:
            counters["grounded_checked"] += 1
            counters["grounded_ok"] += int(verdict.grounded)
            if not verdict.grounded:
                problems.append("judge: not supported by sources: " + verdict.reason)
    return {"id": case["id"], "question": case["question"], "output": out, "counters": counters,
            "problems": problems, "scores": {k: v for k, v in metrics(counters).items() if k in QUESTION_METRICS}}


# ---------- LangSmith ----------

def _dataset(client, name: str, description: str, examples: list[dict]) -> str:
    """Create the dataset, or recreate it when the golden answers changed (version hash in the description)."""
    version = hashlib.sha256(json.dumps(examples, sort_keys=True).encode()).hexdigest()[:12]
    if client.has_dataset(dataset_name=name):
        if version in (client.read_dataset(dataset_name=name).description or ""):
            return name
        client.delete_dataset(dataset_name=name)
    ds = client.create_dataset(name, description=f"{description} [golden set {version}]")
    client.create_examples(dataset_id=ds.id, examples=examples)
    return name


def _evaluators(keys: list[str]):
    def make(key):
        def evaluator(outputs: dict) -> dict:
            return {"key": key, "score": (outputs.get("scores") or {}).get(key)}
        evaluator.__name__ = key
        return evaluator
    return [make(k) for k in keys]


def run_in_langsmith(ws: Workspace, suite: str, cases: list[dict], runner, keys: list[str], collected: list) -> str:
    from langsmith import Client, evaluate

    client = Client()
    by_id = {c["id"]: c for c in cases}
    name = _dataset(client, f"MeetMind eval: {suite}",
                    "Golden " + suite + " for MeetMind (see backend/evals/data)",
                    [{"inputs": {"case_id": c["id"]}, "outputs": {k: v for k, v in c.items() if k != "id"}} for c in cases])

    def target(inputs: dict) -> dict:
        result = runner(ws, by_id[inputs["case_id"]])
        collected.append(result)
        return {"output": result["output"], "scores": result["scores"], "problems": result["problems"]}

    models = ws.models()
    run = evaluate(target, data=name, evaluators=_evaluators(keys), client=client, max_concurrency=1,
                   experiment_prefix=f"{suite}-{(models['analysis'] or ['none'])[0].split('/')[-1]}",
                   metadata={**models, "suite": suite})
    return getattr(run, "experiment_name", "")


# ---------- report ----------

def scorecard(title: str, results: list[dict], keys: list[str], thresholds: dict) -> tuple[dict, list[str]]:
    total = Counter()
    for r in results:
        total.update(r["counters"])
    scores = {k: v for k, v in metrics(total).items() if k in keys}
    print(f"\n{title}")
    print("-" * 78)
    for key in keys:
        score, minimum = scores[key], thresholds.get(key)
        shown = "  n/a" if score is None else f"{score:5.0%}"
        status = "" if score is None or minimum is None else ("PASS" if score >= minimum else "FAIL")
        limit = f"(min {minimum:.0%})" if minimum is not None else ""
        print(f"  {key:<22}{shown}  {limit:<11}{status:<5} {METRICS[key][2]}")
    for r in results:
        for p in r["problems"]:
            print(f"    · {r['id']}: {p}")
    return scores, failures(scores, {k: v for k, v in thresholds.items() if k in keys})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score MeetMind's AI on the golden set.")
    parser.add_argument("--suite", choices=["all", "meetings", "questions"], default="all")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--langsmith", action="store_true", help="also record the run as a LangSmith experiment")
    parser.add_argument("--no-fail", action="store_true", help="always exit 0 (just report)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    only = set(args.only.split(",")) if args.only else None
    thresholds = json.loads((HERE / "thresholds.json").read_text(encoding="utf-8"))
    questions = json.loads((DATA / "questions.json").read_text(encoding="utf-8"))
    ws = Workspace()
    meeting_cases = [c for c in ws.cases if not only or c["id"] in only or args.suite == "questions"]
    question_cases = [q for q in questions if not only or q["id"] in only]
    print(f"MeetMind evaluation · models: {json.dumps(ws.models())}")

    report = {"at": datetime.now(timezone.utc).isoformat(), "models": ws.models(), "suites": {}}
    failed: list[str] = []
    started = time.monotonic()

    # meetings first: the questions are asked about these meetings
    meeting_results: list[dict] = []
    if args.langsmith and args.suite in ("all", "meetings"):
        report["langsmith_meetings"] = run_in_langsmith(ws, "meetings", meeting_cases, run_meeting_case,
                                                        MEETING_METRICS, meeting_results)
    else:
        for case in meeting_cases:
            print(f"  meeting  {case['id']} …", flush=True)
            meeting_results.append(run_meeting_case(ws, case))
    if args.suite in ("all", "meetings"):
        scores, bad = scorecard("MEETINGS: summary, decisions, Q&A, tasks", meeting_results, MEETING_METRICS, thresholds)
        report["suites"]["meetings"] = {"scores": scores, "cases": meeting_results}
        failed += bad

    if args.suite in ("all", "questions"):
        ws.build_search()
        question_results: list[dict] = []
        if args.langsmith:
            report["langsmith_questions"] = run_in_langsmith(ws, "questions", question_cases, run_question_case,
                                                             QUESTION_METRICS, question_results)
        else:
            for case in question_cases:
                print(f"  question {case['id']} …", flush=True)
                question_results.append(run_question_case(ws, case))
        scores, bad = scorecard("QUESTIONS: Ask MeetMind (agent + RAG)", question_results, QUESTION_METRICS, thresholds)
        report["suites"]["questions"] = {"scores": scores, "cases": question_results}
        failed += bad

    report["seconds"] = round(time.monotonic() - started, 1)
    report["failed"] = failed
    RESULTS.mkdir(exist_ok=True)
    text = json.dumps(report, indent=2, default=lambda o: dict(o) if isinstance(o, Counter) else str(o))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"{stamp}.json").write_text(text, encoding="utf-8")
    (RESULTS / "latest.json").write_text(text, encoding="utf-8")
    print(f"\nTook {report['seconds']} s · report: evals/results/{stamp}.json")
    if report.get("langsmith_meetings") or report.get("langsmith_questions"):
        print("LangSmith experiments:", report.get("langsmith_meetings", "-"), "|", report.get("langsmith_questions", "-"))
    if failed:
        print("BELOW THRESHOLD:\n  " + "\n  ".join(failed))
        return 0 if args.no_fail else 1
    print("All scores meet their thresholds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
