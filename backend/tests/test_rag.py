"""RAG tests: chunking, indexing, access control, hybrid search, cross-meeting answers."""
import pytest

from conftest import add_member, auth, create_meeting, signup
from meetmind.ai import rag as rag_module
from meetmind.ai.pipeline import run_pipeline
from meetmind.ai.rag import chunk_transcript, keyword_terms
from meetmind.ai.transcription import TranscriptionResult
from test_ai import ANALYSIS, make_deps, upload


def _transcript(*lines):
    segs = [{"start": float(i * 10), "end": float(i * 10 + 9), "text": t} for i, t in enumerate(lines)]
    return TranscriptionResult(" ".join(lines), segs, "en", "fake-whisper")


PRICING = _transcript(
    "Let's talk about pricing for the enterprise plan.",
    "We agreed the enterprise plan costs 49 euros per seat.",
    "Discounts above 20 percent need approval from finance.",
)
HIRING = _transcript(
    "Hiring update: we are opening two backend roles.",
    "Interviews start in January with the platform team.",
)


def _processed_meeting(app, client, admin, title, transcript, participants=("maya@acme.com",)):
    m = create_meeting(client, admin, participants=list(participants), title=title)
    run_pipeline(make_deps(app, transcript=transcript, analysis=ANALYSIS.model_copy(update={"tasks": []})),
                 upload(client, admin, m["id"]))
    return m["id"]


@pytest.fixture
def library(app, client, invites, enqueued):
    """Two processed meetings; Maya only attended the pricing one."""
    admin = signup(client)
    member = add_member(client, admin, invites)
    pricing = _processed_meeting(app, client, admin, "Pricing review", PRICING)
    hiring = _processed_meeting(app, client, admin, "Hiring sync", HIRING, participants=("bob@acme.com",))
    return {"admin": admin, "member": member, "pricing": pricing, "hiring": hiring}


# ---------- chunking ----------

def test_chunks_keep_timestamps_and_overlap():
    segs = [{"start": float(i * 30), "end": float(i * 30 + 29), "text": f"sentence number {i} " * 5} for i in range(10)]
    chunks = chunk_transcript(segs, "", max_chars=300, overlap=1)
    assert len(chunks) > 1
    assert chunks[0][1] == 0.0 and chunks[0][0].startswith("[00:00:00]")
    # the last segment of one chunk is repeated at the start of the next one
    assert chunks[0][0].splitlines()[-1] == chunks[1][0].splitlines()[0]
    assert chunks[-1][2] == 299.0


def test_chunks_without_segments():
    chunks = chunk_transcript(None, "word " * 1000, max_chars=500)
    assert len(chunks) > 5 and all(start is None for _, start, _ in chunks)


def test_keyword_terms():
    terms = keyword_terms("What did Maya decide about the enterprise pricing?")
    assert "Maya" in terms and "maya" in terms and "enterprise" in terms
    assert "what" not in [t.lower() for t in terms] and "the" not in terms


# ---------- indexing ----------

def test_pipeline_indexes_notes_and_transcript(app, library):
    items = app.state.rag.store.items
    pricing = [c for c, _ in items.values() if c.metadata["meeting_id"] == library["pricing"]]
    kinds = sorted(c.metadata["kind"] for c in pricing)
    assert kinds[0] == "notes" and "transcript" in kinds
    meta = pricing[0].metadata
    assert meta["meeting_title"] == "Pricing review" and meta["timezone"] == "UTC"
    assert all(v is not None for v in meta.values())  # Chroma rejects None values


def test_reprocessing_replaces_chunks(app, client, library, enqueued):
    before = len(app.state.rag.store.items)
    run_pipeline(make_deps(app, transcript=PRICING, analysis=ANALYSIS.model_copy(update={"tasks": []})),
                 upload(client, library["admin"], library["pricing"]))
    assert len(app.state.rag.store.items) == before


def test_deleting_meeting_removes_it_from_search(app, client, library):
    client.delete(f"/api/meetings/{library['pricing']}", headers=auth(library["admin"]))
    assert all(c.metadata["meeting_id"] != library["pricing"] for c, _ in app.state.rag.store.items.values())


def test_indexing_failure_is_retried_in_background(app, client, invites, enqueued, monkeypatch):
    class Broken:
        def embed_documents(self, texts):
            raise ConnectionError("embedding service down")

        def embed_query(self, text):
            raise ConnectionError("down")

    app.state.rag.embedder = Broken()
    monkeypatch.setattr(rag_module.time, "sleep", lambda s: None)  # skip the retry back-off in tests
    admin = signup(client)
    m = create_meeting(client, admin)
    run_pipeline(make_deps(app), upload(client, admin, m["id"]))
    meeting = client.get(f"/api/meetings/{m['id']}", headers=auth(admin)).json()["meeting"]
    assert meeting["status"] == "review"  # the meeting still finished
    assert ("meetmind.jobs.index_meeting", (m["id"],)) in enqueued


# ---------- search & access control ----------

def test_search_finds_relevant_passage(client, library):
    res = client.get("/api/search", params={"q": "how much does the enterprise plan cost"}, headers=auth(library["admin"]))
    assert res.status_code == 200
    top = res.json()["items"][0]
    assert top["meeting_title"] == "Pricing review" and "49 euros" in top["text"]


def test_members_only_search_meetings_they_attended(client, library):
    res = client.get("/api/search", params={"q": "hiring backend roles interviews"}, headers=auth(library["member"]))
    titles = {i["meeting_title"] for i in res.json()["items"]}
    assert "Hiring sync" not in titles  # Maya wasn't in that meeting


def test_other_organizations_never_appear(client, library):
    other = signup(client, email="admin@globex.com", org="Globex")
    res = client.get("/api/search", params={"q": "enterprise pricing"}, headers=auth(other))
    assert res.json()["items"] == []


def test_keyword_search_still_works_when_embeddings_fail(app, client, library):
    class Down:
        def embed_query(self, text):
            raise ConnectionError("503 overloaded")

    app.state.rag.embedder = Down()
    res = client.get("/api/search", params={"q": "Discounts finance"}, headers=auth(library["admin"]))
    assert res.status_code == 200
    assert any("Discounts" in i["text"] for i in res.json()["items"])


# ---------- answers ----------

def test_ask_across_meetings_returns_answer_with_sources(client, library, monkeypatch):
    seen = {}

    def fake_answer(self, question, hits, history=None):
        seen["hits"] = hits
        return "The enterprise plan costs 49 euros per seat [1].", "fake:chat"

    monkeypatch.setattr(rag_module.RAGService, "answer", fake_answer)
    res = client.post("/api/search/ask", json={"question": "What does the enterprise plan cost?"},
                      headers=auth(library["admin"]))
    assert res.status_code == 200, res.json()
    body = res.json()
    assert body["model"] == "fake:chat" and "[1]" in body["answer"]
    assert body["sources"][0]["n"] == 1 and body["sources"][0]["meeting_id"] == library["pricing"]
    assert "Pricing review" in rag_module.source_label(seen["hits"][0])


def test_ask_limited_to_one_meeting_and_access_checked(client, library, monkeypatch):
    monkeypatch.setattr(rag_module.RAGService, "answer", lambda self, q, hits, history=None: ("ok", "fake"))
    res = client.post("/api/search/ask", json={"question": "roles", "meeting_ids": [library["hiring"]]},
                      headers=auth(library["admin"]))
    assert {s["meeting_id"] for s in res.json()["sources"]} == {library["hiring"]}
    # a member can't target a meeting they didn't attend
    res = client.post("/api/search/ask", json={"question": "roles", "meeting_ids": [library["hiring"]]},
                      headers=auth(library["member"]))
    assert res.status_code == 404


def test_ask_without_matches_does_not_call_the_model(client, invites, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("model should not be called")

    monkeypatch.setattr(rag_module.RAGService, "answer", boom)
    admin = signup(client)
    res = client.post("/api/search/ask", json={"question": "anything"}, headers=auth(admin))
    assert res.status_code == 200 and res.json()["sources"] == [] and "couldn't find" in res.json()["answer"]


def test_long_meeting_question_uses_retrieved_excerpts(app, client, library, monkeypatch, settings):
    from meetmind.ai import qa

    settings.rag_full_context_chars = 50  # treat every meeting as "long"
    seen = {}

    def fake_answer(s, context, question):
        seen["ctx"] = context
        return "ok", "fake"

    monkeypatch.setattr(qa, "answer_question", fake_answer)
    res = client.post(f"/api/meetings/{library['pricing']}/ask", json={"question": "enterprise plan seat price"},
                      headers=auth(library["admin"]))
    assert res.status_code == 200
    assert "RELEVANT TRANSCRIPT EXCERPTS" in seen["ctx"] and "49 euros" in seen["ctx"]


def test_sources_keep_zero_timestamp_and_only_cited_are_returned(client, library, monkeypatch):
    monkeypatch.setattr(rag_module.RAGService, "answer", lambda self, q, hits, history=None: ("Price is 49 euros [2].", "fake"))
    res = client.post("/api/search/ask", json={"question": "enterprise plan cost euros seat"},
                      headers=auth(library["admin"]))
    sources = res.json()["sources"]
    assert [s["n"] for s in sources] == [2]  # only the cited source, original number kept
    hits = client.get("/api/search", params={"q": "enterprise pricing"}, headers=auth(library["admin"])).json()["items"]
    first_chunk = next(h for h in hits if h["kind"] == "transcript")
    assert first_chunk["start"] == 0.0  # a chunk starting at 0 s still has a timestamp
