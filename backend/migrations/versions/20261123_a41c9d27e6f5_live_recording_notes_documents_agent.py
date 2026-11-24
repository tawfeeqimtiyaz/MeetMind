"""Live recording, meeting notes, documents, task priority/reminders, follow-ups, agent runs

Revision ID: a41c9d27e6f5
Revises: 8c2f4e1a7b93
Create Date: 2026-11-23 09:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a41c9d27e6f5"
down_revision: Union[str, None] = "8c2f4e1a7b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("meetings", sa.Column("follow_ups", JSONType, nullable=True))

    op.add_column("recordings", sa.Column("mode", sa.String(length=20), nullable=False, server_default="upload"))
    op.add_column("recordings", sa.Column("duration_seconds", sa.Float(), nullable=True))
    op.add_column("recordings", sa.Column("important", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("recordings", sa.Column("chunks_received", sa.Integer(), nullable=False, server_default="0"))

    op.add_column("tasks", sa.Column("priority", sa.String(length=10), nullable=False, server_default="normal"))
    op.add_column("tasks", sa.Column("due_reminder_sent_on", sa.Date(), nullable=True))
    op.add_column("tasks", sa.Column("overdue_reminder_sent_on", sa.Date(), nullable=True))

    op.create_table(
        "meeting_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("offset_seconds", sa.Float(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_notes_meeting_id", "meeting_notes", ["meeting_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("pages", JSONType, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_meeting_id", "documents", ["meeting_id"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("plan", JSONType, nullable=True),
        sa.Column("result", JSONType, nullable=True),
        sa.Column("ai_model", sa.String(length=120), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_organization_id", "agent_runs", ["organization_id"])
    op.create_index("ix_agent_runs_meeting_id", "agent_runs", ["meeting_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_meeting_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_organization_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_documents_meeting_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_meeting_notes_meeting_id", table_name="meeting_notes")
    op.drop_table("meeting_notes")
    for col in ("overdue_reminder_sent_on", "due_reminder_sent_on", "priority"):
        op.drop_column("tasks", col)
    for col in ("chunks_received", "important", "duration_seconds", "mode"):
        op.drop_column("recordings", col)
    op.drop_column("meetings", "follow_ups")
