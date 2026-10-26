"""policies: business period start/end

Revision ID: 8c41d2e7a9f0
Revises: 3f2a9c1d7b4e
Create Date: 2026-10-26 15:40:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8c41d2e7a9f0"
down_revision: str | Sequence[str] | None = "3f2a9c1d7b4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("policies", sa.Column("biz_start", sa.Date(), nullable=True))
    op.add_column("policies", sa.Column("biz_end", sa.Date(), nullable=True))
    op.create_index("ix_policies_biz_end", "policies", ["biz_end"])


def downgrade() -> None:
    op.drop_index("ix_policies_biz_end", table_name="policies")
    op.drop_column("policies", "biz_end")
    op.drop_column("policies", "biz_start")
