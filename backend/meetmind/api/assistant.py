"""Task assistant endpoints: plan from an instruction, then confirm or cancel (human-in-the-loop)."""
import logging
import uuid

from fastapi import APIRouter
from sqlalchemy import select, update

from .. import notifications
from ..ai import default_deps, task_agent
from ..ai.llm import AIUnavailableError
from ..ai.pipeline import resume_review
from ..deps import AdminUser, DbSession, SettingsDep
from ..errors import APIError
from ..models import AgentRun, Meeting
from ..ratelimit import rate_limit
from ..schemas import AssistantIn
from ..serializers import meeting_out
from .common import get_meeting_for

router = APIRouter()
log = logging.getLogger(__name__)


def run_out(r: AgentRun) -> dict:
    return {
        "id": str(r.id),
        "meeting_id": str(r.meeting_id),
        "instruction": r.instruction,
        "status": r.status,
        "plan": r.plan,
        "result": r.result,
        "ai_model": r.ai_model,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/meetings/{meeting_id}/assistant/plan", status_code=201,
             dependencies=[rate_limit("20/minute", "assistant")])
def plan(meeting_id: uuid.UUID, data: AssistantIn, admin: AdminUser, db: DbSession, settings: SettingsDep):
    """Turn the admin's instruction into a plan. Nothing is changed yet."""
    meeting = get_meeting_for(db, admin, meeting_id)
    try:
        proposed, model = task_agent.propose(settings, db, meeting, data.instruction)
    except AIUnavailableError as exc:
        raise APIError(503, "ai_unavailable", str(exc))
    checked = task_agent.validate(settings, db, meeting, proposed)
    run = AgentRun(organization_id=meeting.organization_id, meeting_id=meeting.id, user_id=admin.id,
                   instruction=data.instruction, status="proposed", plan=checked, ai_model=model)
    db.add(run)
    db.commit()
    return {"run": run_out(run)}


def _get_run(db, admin, run_id) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None or run.organization_id != admin.organization_id:
        raise APIError(404, "not_found", "Assistant run not found")
    return run


@router.post("/agent-runs/{run_id}/confirm")
def confirm(run_id: uuid.UUID, admin: AdminUser, db: DbSession, settings: SettingsDep):
    """Apply a proposed plan: assign/approve/create tasks, then email the owners."""
    run = _get_run(db, admin, run_id)
    claimed = db.execute(update(AgentRun).where(AgentRun.id == run.id, AgentRun.status == "proposed")
                         .values(status="applying")).rowcount
    db.commit()
    if claimed != 1:
        raise APIError(409, "not_pending", "This plan was already confirmed or cancelled")
    meeting = get_meeting_for(db, admin, run.meeting_id)
    try:
        applied = task_agent.apply(db, meeting, admin, run.plan or {})
        run.status = "done"
        run.result = {"changed": len(applied.changed), "discarded": applied.discarded}
        db.commit()
    except Exception:
        db.rollback()
        db.execute(update(AgentRun).where(AgentRun.id == run.id).values(status="failed"))
        db.commit()
        log.exception("Applying assistant run %s failed", run.id)
        raise APIError(500, "apply_failed", "Could not apply the plan. Nothing was changed.")

    # All AI drafts handled -> close the paused review in the LangGraph pipeline
    if meeting.status == "review" and meeting.ai_thread_id and all(t.approved for t in meeting.tasks):
        thread_id = meeting.ai_thread_id
        if db.execute(update(Meeting).where(Meeting.id == meeting.id, Meeting.ai_thread_id == thread_id)
                      .values(ai_thread_id=None)).rowcount == 1:
            db.commit()
            try:
                resume_review(default_deps(settings, _factory(db)), thread_id,
                              [str(t.id) for t in meeting.tasks])
            except Exception:  # noqa: BLE001 - tasks are saved; just mark the meeting done
                log.warning("Could not resume review thread %s; completing the meeting directly", thread_id)
                db.execute(update(Meeting).where(Meeting.id == meeting.id).values(status="completed"))
                db.commit()

    if (run.plan or {}).get("notify_owners") and applied.changed:
        notifications.notify_tasks_assigned(settings, meeting, applied.changed,
                                            message=run.plan.get("message"), assigned_by=admin)
    db.expire_all()
    meeting = get_meeting_for(db, admin, run.meeting_id)
    return {"run": run_out(_get_run(db, admin, run_id)), "meeting": meeting_out(meeting, list(meeting.tasks))}


def _factory(db):
    from ..db import make_session_factory

    return make_session_factory(db.get_bind())


@router.post("/agent-runs/{run_id}/cancel")
def cancel(run_id: uuid.UUID, admin: AdminUser, db: DbSession):
    run = _get_run(db, admin, run_id)
    if run.status != "proposed":
        raise APIError(409, "not_pending", "This plan was already confirmed or cancelled")
    run.status = "cancelled"
    db.commit()
    return {"run": run_out(run)}


@router.get("/meetings/{meeting_id}/assistant/runs")
def history(meeting_id: uuid.UUID, admin: AdminUser, db: DbSession):
    """Audit log: every instruction given to the assistant for this meeting and what happened."""
    get_meeting_for(db, admin, meeting_id)
    runs = db.scalars(select(AgentRun).where(AgentRun.meeting_id == meeting_id)
                      .order_by(AgentRun.created_at.desc()).limit(50)).all()
    return {"items": [run_out(r) for r in runs]}
