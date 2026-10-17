"""initial schema: policies, saving products, financial companies, ingest runs

Revision ID: 3f2a9c1d7b4e
Revises:
Create Date: 2026-10-17 11:50:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3f2a9c1d7b4e"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _text_array_default() -> sa.TextClause:
    return sa.text("'{}'")


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("plcy_no", sa.String(length=30), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "keywords",
            postgresql.ARRAY(sa.Text()),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_l", sa.String(length=50), nullable=True),
        sa.Column("category_m", sa.String(length=100), nullable=True),
        sa.Column("support_content", sa.Text(), nullable=True),
        sa.Column("min_age", sa.Integer(), nullable=True),
        sa.Column("max_age", sa.Integer(), nullable=True),
        sa.Column("age_limited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("marriage_code", sa.String(length=10), nullable=True),
        sa.Column("earn_cond_code", sa.String(length=10), nullable=True),
        sa.Column("earn_min", sa.BigInteger(), nullable=True),
        sa.Column("earn_max", sa.BigInteger(), nullable=True),
        sa.Column("earn_etc", sa.Text(), nullable=True),
        sa.Column(
            "job_codes",
            postgresql.ARRAY(sa.String(length=10)),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column(
            "school_codes",
            postgresql.ARRAY(sa.String(length=10)),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column(
            "special_codes",
            postgresql.ARRAY(sa.String(length=10)),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column(
            "major_codes",
            postgresql.ARRAY(sa.String(length=10)),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column(
            "sigungu_codes",
            postgresql.ARRAY(sa.String(length=5)),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column(
            "sido_codes",
            postgresql.ARRAY(sa.String(length=2)),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column("apply_period_code", sa.String(length=10), nullable=True),
        sa.Column("apply_start", sa.Date(), nullable=True),
        sa.Column("apply_end", sa.Date(), nullable=True),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column(
            "ref_urls",
            postgresql.ARRAY(sa.Text()),
            server_default=_text_array_default(),
            nullable=False,
        ),
        sa.Column("apply_method", sa.Text(), nullable=True),
        sa.Column("screening_method", sa.Text(), nullable=True),
        sa.Column("documents", sa.Text(), nullable=True),
        sa.Column("extra_qualification", sa.Text(), nullable=True),
        sa.Column("exclusion", sa.Text(), nullable=True),
        sa.Column("org_name", sa.Text(), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("plcy_no", name=op.f("pk_policies")),
    )
    op.create_index("ix_policies_sido_codes", "policies", ["sido_codes"], postgresql_using="gin")
    op.create_index("ix_policies_job_codes", "policies", ["job_codes"], postgresql_using="gin")
    op.create_index("ix_policies_apply_end", "policies", ["apply_end"])

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

    op.create_table(
        "ingest_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("upserted_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_runs")),
    )
    op.create_index("ix_ingest_runs_source_started_at", "ingest_runs", ["source", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_ingest_runs_source_started_at", table_name="ingest_runs")
    op.drop_table("ingest_runs")
    op.drop_table("saving_product_options")
    op.drop_index("ix_saving_products_is_youth", table_name="saving_products")
    op.drop_table("saving_products")
    op.drop_table("financial_companies")
    op.drop_index("ix_policies_apply_end", table_name="policies")
    op.drop_index("ix_policies_job_codes", table_name="policies")
    op.drop_index("ix_policies_sido_codes", table_name="policies")
    op.drop_table("policies")
