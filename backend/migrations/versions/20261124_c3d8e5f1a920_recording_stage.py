"""Processing stage of a recording (shown as progress steps on the meeting page)

Revision ID: c3d8e5f1a920
Revises: a41c9d27e6f5
Create Date: 2026-11-24 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8e5f1a920"
down_revision: Union[str, None] = "a41c9d27e6f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("recordings", sa.Column("stage", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("recordings", "stage")
