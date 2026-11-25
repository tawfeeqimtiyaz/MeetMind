"""The evaluation scorers (the 'answer key' checks) must themselves be right."""
import json
from pathlib import Path

from evals.scoring import failures, metrics, score_answer, score_meeting

DATA = Path(__file__).resolve().parents[1] / "evals" / "data"

EXPECTED = {
    "tasks": [
        {"match": ["api doc"], "owner": "maya@acme.test", "due": "2026-12-04"},
        {"match": ["demo"], "owner": "jordan@acme.test", "due": "2026-12-11", "priority": "urgent"},
    ],
    "decisions": [["calendar sync", "january 12"]],
    "follow_ups": [["payment bug"]],
    "open_questions": [["pricing"]],
    "summary_must": ["january 12"],
    "not_tasks": ["desktop app"],
}


def test_perfect_meeting_scores_100():
    output = {
        "summary": "We launch on January 12 without calendar sync.",
        "decisions": ["Calendar sync moves to v2 so we can launch on January 12"],
        "follow_ups": [{"question": "Is the payment bug fixed?", "answer": "No"}],
        "open_questions": ["Who owns the pricing page copy?"],
        "tasks": [
            {"title": "Finish the API documentation", "owner": "maya@acme.test", "due": "2026-12-04", "priority": "normal"},
            {"title": "Book the stakeholder demo", "owner": "JORDAN@acme.test", "due": "2026-12-11", "priority": "urgent"},
        ],
    }
    counters, problems = score_meeting(EXPECTED, output)
    scores = metrics(counters)
    assert problems == []
    assert all(scores[k] == 1.0 for k in ("task_recall", "task_precision", "owner_accuracy", "due_date_accuracy",
                                         "priority_accuracy", "no_invented_tasks", "decision_recall",
                                         "followup_recall", "open_question_recall", "summary_facts"))
    assert scores["answer_correct"] is None  # not measured here


def test_mistakes_are_counted_and_explained():
    output = {
        "summary": "Launch discussed.",
        "decisions": ["Launch on January 12"],  # calendar sync missing -> decision not found
        "follow_ups": [],
        "open_questions": [],
        "tasks": [
            {"title": "Finish the API docs", "owner": "jordan@acme.test", "due": "2026-12-05", "priority": "normal"},
            {"title": "Build a desktop app", "owner": None, "due": None, "priority": "normal"},  # parked idea
        ],
    }
    counters, problems = score_meeting(EXPECTED, output)
    scores = metrics(counters)
    assert scores["task_recall"] == 0.5 and scores["task_precision"] == 0.5
    assert scores["owner_accuracy"] == 0.0 and scores["due_date_accuracy"] == 0.0
    assert scores["no_invented_tasks"] == 0.0 and scores["decision_recall"] == 0.0 and scores["summary_facts"] == 0.0
    text = " ".join(problems)
    assert "missing task: 'demo'" in text and "should not be a task" in text and "expected 'maya@acme.test'" in text


def test_answer_scoring_with_sources_and_unknowns():
    case = {"must": [["22"]], "source": {"meeting": "Release planning"}}
    good, _ = score_answer(case, "The beta launches on December 22.", [{"kind": "transcript", "meeting_title": "Release planning"}])
    assert metrics(good)["answer_correct"] == 1.0 and metrics(good)["source_hit"] == 1.0
    wrong, problems = score_answer(case, "On December 15.", [{"kind": "notes", "meeting_title": "Hiring sync"}])
    assert metrics(wrong)["answer_correct"] == 0.0 and metrics(wrong)["source_hit"] == 0.0 and len(problems) == 2

    unknown = {"must": [["couldn", "not find"]], "unanswerable": True}
    c, _ = score_answer(unknown, "I couldn't find that in your meetings.", [])
    assert metrics(c)["says_when_unknown"] == 1.0
    c, _ = score_answer(unknown, "Paris.", [])
    assert metrics(c)["says_when_unknown"] == 0.0


def test_judge_sees_the_source_labels():
    from evals.scoring import source_header

    label = source_header({"meeting_title": "Release planning", "scheduled_at": "2026-12-01T09:30:00+00:00", "kind": "slide"})
    assert label == "(meeting: Release planning, date: 2026-12-01, slide)"
    doc = source_header({"meeting_title": "Incident", "kind": "document", "document_name": "bug.md", "page": 1})
    assert "document 'bug.md', page 1" in doc


def test_thresholds_report_only_real_drops():
    assert failures({"task_recall": 0.8, "grounded": None, "owner_accuracy": 1.0},
                    {"task_recall": 0.85, "grounded": 0.8, "owner_accuracy": 0.9}) == ["task_recall: 80% < 85%"]


def test_golden_set_is_well_formed():
    meetings = json.loads((DATA / "meetings.json").read_text(encoding="utf-8"))
    questions = json.loads((DATA / "questions.json").read_text(encoding="utf-8"))
    assert len({m["id"] for m in meetings}) == len(meetings) >= 5
    assert len({q["id"] for q in questions}) == len(questions) >= 10
    for m in meetings:
        emails = {e for _, e in m["participants"]}
        assert all(t.get("owner", next(iter(emails))) in emails for t in m["expected"]["tasks"]), m["id"]
        assert all(isinstance(t["match"], list) and t["match"] for t in m["expected"]["tasks"])
    for q in questions:
        assert q["must"] and all(isinstance(g, list) and g for g in q["must"]), q["id"]
