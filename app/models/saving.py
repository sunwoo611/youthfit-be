from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IngestedAtMixin


class FinancialCompany(IngestedAtMixin, Base):
    """금감원 companySearch. 적금 상품에 URL 필드가 없어 홈페이지를 공식 URL로 쓴다."""

    __tablename__ = "financial_companies"

    fin_co_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    fin_grp_code: Mapped[str | None] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(Text)
    call_center: Mapped[str | None] = mapped_column(String(50))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SavingProduct(IngestedAtMixin, Base):
    """금감원 savingProductsSearch baseList.

    fin_co_no 는 financial_companies 에 FK 를 걸지 않는다. companySearch 누락 시에도
    상품 적재가 실패하지 않게 하고, 매핑 성공률은 품질 리포트에서 측정한다.
    """

    __tablename__ = "saving_products"

    fin_co_no: Mapped[str] = mapped_column(String(20), primary_key=True)
    fin_prdt_cd: Mapped[str] = mapped_column(String(50), primary_key=True)
    fin_grp_code: Mapped[str] = mapped_column(String(10), nullable=False)
    company_name: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    join_way: Mapped[str | None] = mapped_column(Text)
    special_condition: Mapped[str | None] = mapped_column(Text)
    join_deny: Mapped[str | None] = mapped_column(String(1))
    join_member: Mapped[str | None] = mapped_column(Text)
    etc_note: Mapped[str | None] = mapped_column(Text)
    max_limit: Mapped[int | None] = mapped_column(BigInteger)
    dcls_month: Mapped[str | None] = mapped_column(String(6))
    is_youth: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    options: Mapped[list["SavingProductOption"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("ix_saving_products_is_youth", "is_youth"),)


class SavingProductOption(Base):
    """금감원 savingProductsSearch optionList (기간·적립방식별 금리)."""

    __tablename__ = "saving_product_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fin_co_no: Mapped[str] = mapped_column(String(20), nullable=False)
    fin_prdt_cd: Mapped[str] = mapped_column(String(50), nullable=False)
    rate_type: Mapped[str] = mapped_column(String(1), nullable=False)
    rate_type_name: Mapped[str | None] = mapped_column(String(20))
    reserve_type: Mapped[str] = mapped_column(String(1), nullable=False)
    reserve_type_name: Mapped[str | None] = mapped_column(String(20))
    term_months: Mapped[int] = mapped_column(Integer, nullable=False)
    base_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    max_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    product: Mapped[SavingProduct] = relationship(back_populates="options")

    __table_args__ = (
        ForeignKeyConstraint(
            ["fin_co_no", "fin_prdt_cd"],
            ["saving_products.fin_co_no", "saving_products.fin_prdt_cd"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "fin_co_no",
            "fin_prdt_cd",
            "rate_type",
            "reserve_type",
            "term_months",
            name="uq_saving_product_options_key",
        ),
    )
