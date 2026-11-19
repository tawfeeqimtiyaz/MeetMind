"""Meeting time zone (due dates are resolved in the meeting's local calendar)

Revision ID: 8c2f4e1a7b93
Revises: 5b1e7c9a2d40
Create Date: 2026-11-20 03:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8c2f4e1a7b93"
down_revision: Union[str, None] = "5b1e7c9a2d40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("timezone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("meetings", "timezone")
