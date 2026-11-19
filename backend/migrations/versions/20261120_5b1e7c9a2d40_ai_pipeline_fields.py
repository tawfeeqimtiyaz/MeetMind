"""AI pipeline fields: open questions, model/thread on meetings; evidence/owner on tasks

Revision ID: 5b1e7c9a2d40
Revises: 067aed3411cd
Create Date: 2026-11-20 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5b1e7c9a2d40"
down_revision: Union[str, None] = "067aed3411cd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("meetings", sa.Column("open_questions", JSONType, nullable=True))
    op.add_column("meetings", sa.Column("ai_model", sa.String(length=120), nullable=True))
    op.add_column("meetings", sa.Column("ai_thread_id", sa.String(length=100), nullable=True))
    op.add_column("tasks", sa.Column("evidence", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("suggested_owner", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "suggested_owner")
    op.drop_column("tasks", "evidence")
    op.drop_column("meetings", "ai_thread_id")
    op.drop_column("meetings", "ai_model")
    op.drop_column("meetings", "open_questions")
