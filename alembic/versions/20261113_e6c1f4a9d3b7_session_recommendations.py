"""sessions: recommendation reason cache and answers revision

Revision ID: e6c1f4a9d3b7
Revises: d4a8c2f6b1e9
Create Date: 2026-11-13 14:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e6c1f4a9d3b7"
down_revision: str | Sequence[str] | None = "d4a8c2f6b1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("answers_revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "sessions",
        sa.Column("recommendations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "recommendations")
    op.drop_column("sessions", "answers_revision")
