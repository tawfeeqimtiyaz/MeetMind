"""Search and questions across all meetings the user can see (RAG)."""
import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from ..ai.llm import AIUnavailableError
from ..ai.rag import RAGService, cited_numbers, hit_out
from ..deps import CurrentUser, DbSession, get_rag
from ..errors import APIError
from ..models import Meeting, MeetingParticipant, User
from ..ratelimit import rate_limit
from ..schemas import AskAllIn
from .common import visible_meetings

router = APIRouter()
log = logging.getLogger(__name__)

NOT_FOUND = "I couldn't find anything about that in your meetings."


def _scope(db, user: User, requested=None) -> list[str] | None:
    """Meetings the search may use: None = whole organization (admins), else an explicit list."""
    if requested:
        ids = [str(i) for i in db.scalars(visible_meetings(user).where(Meeting.id.in_(requested)).with_only_columns(Meeting.id))]
        if len(ids) != len(set(requested)):
            raise APIError(404, "not_found", "Meeting not found")
        return ids
    if user.role == "admin":
        return None
    return [str(i) for i in db.scalars(
        select(MeetingParticipant.meeting_id).join(Meeting)
        .where(MeetingParticipant.user_id == user.id, Meeting.organization_id == user.organization_id)
    )]


@router.get("")
def search(
    user: CurrentUser, db: DbSession, rag: RAGService = Depends(get_rag),
    q: str = Query(min_length=1, max_length=500), limit: int = Query(8, ge=1, le=30),
):
    """Find the most relevant meeting passages (no AI answer, just the matches)."""
    try:
        hits = rag.search(q, str(user.organization_id), _scope(db, user), k=limit)
    except AIUnavailableError as exc:
        raise APIError(503, "search_unavailable", str(exc))
    return {"items": [hit_out(h, i) for i, h in enumerate(hits, 1)]}


@router.post("/ask", dependencies=[rate_limit("20/minute", "ask-all")])
def ask(data: AskAllIn, request: Request, user: CurrentUser, db: DbSession, rag: RAGService = Depends(get_rag)):
    """Answer a question from all meetings (and their documents) the user can see, with numbered sources.
    The multi-tool assistant runs first; if no AI model can run it, plain RAG answers instead."""
    from ..ai.agents import run_assistant, scope_for

    scope_ids = _scope(db, user, data.meeting_ids)
    history = [t.model_dump() for t in data.history]
    try:
        scope = scope_for(request.app.state.session_factory, db, request.app.state.settings, rag, user)
        scope.meeting_ids = scope_ids
        result = run_assistant(scope, data.question, history)
        return {"answer": result.answer, "model": result.model, "sources": result.sources, "tools": result.tools_used}
    except AIUnavailableError:
        log.warning("Assistant unavailable; answering with plain RAG")
    try:
        hits = rag.search(data.question, str(user.organization_id), scope_ids)
    except AIUnavailableError as exc:
        raise APIError(503, "search_unavailable", str(exc))
    if not hits:
        return {"answer": NOT_FOUND, "model": None, "sources": [], "tools": ["rag"]}
    try:
        answer, model = rag.answer(data.question, hits, history)
    except AIUnavailableError as exc:
        raise APIError(503, "ai_unavailable", str(exc))
    sources = [hit_out(h, i) for i, h in enumerate(hits, 1)]
    # Show only the sources the answer actually cites (numbers stay the same, so [n] still matches)
    cited = cited_numbers(answer)
    if cited:
        sources = [s for s in sources if s["n"] in cited]
    return {"answer": answer, "model": model, "sources": sources, "tools": ["rag"]}
