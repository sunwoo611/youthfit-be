"""drop savings: saving products, options, financial companies

Revision ID: d4a8c2f6b1e9
Revises: b7e3f1a2c5d8
Create Date: 2026-11-13 10:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4a8c2f6b1e9"
down_revision: str | Sequence[str] | None = "b7e3f1a2c5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("saving_product_options")
    op.drop_index("ix_saving_products_is_youth", table_name="saving_products")
    op.drop_table("saving_products")
    op.drop_table("financial_companies")


def downgrade() -> None:
    op.create_table(
        "financial_companies",
        sa.Column("fin_co_no", sa.String(length=20), nullable=False),
        sa.Column("fin_grp_code", sa.String(length=10), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("call_center", sa.String(length=50), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("fin_co_no", name=op.f("pk_financial_companies")),
    )

    op.create_table(
        "saving_products",
        sa.Column("fin_co_no", sa.String(length=20), nullable=False),
        sa.Column("fin_prdt_cd", sa.String(length=50), nullable=False),
        sa.Column("fin_grp_code", sa.String(length=10), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("join_way", sa.Text(), nullable=True),
        sa.Column("special_condition", sa.Text(), nullable=True),
        sa.Column("join_deny", sa.String(length=1), nullable=True),
        sa.Column("join_member", sa.Text(), nullable=True),
        sa.Column("etc_note", sa.Text(), nullable=True),
        sa.Column("max_limit", sa.BigInteger(), nullable=True),
        sa.Column("dcls_month", sa.String(length=6), nullable=True),
        sa.Column("is_youth", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("fin_co_no", "fin_prdt_cd", name=op.f("pk_saving_products")),
    )
    op.create_index("ix_saving_products_is_youth", "saving_products", ["is_youth"])

    op.create_table(
        "saving_product_options",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fin_co_no", sa.String(length=20), nullable=False),
        sa.Column("fin_prdt_cd", sa.String(length=50), nullable=False),
        sa.Column("rate_type", sa.String(length=1), nullable=False),
        sa.Column("rate_type_name", sa.String(length=20), nullable=True),
        sa.Column("reserve_type", sa.String(length=1), nullable=False),
        sa.Column("reserve_type_name", sa.String(length=20), nullable=True),
        sa.Column("term_months", sa.Integer(), nullable=False),
        sa.Column("base_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("max_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.ForeignKeyConstraint(
            ["fin_co_no", "fin_prdt_cd"],
            ["saving_products.fin_co_no", "saving_products.fin_prdt_cd"],
            name=op.f("fk_saving_product_options_fin_co_no_saving_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saving_product_options")),
        sa.UniqueConstraint(
            "fin_co_no",
            "fin_prdt_cd",
            "rate_type",
            "reserve_type",
            "term_months",
            name="uq_saving_product_options_key",
        ),
    )
