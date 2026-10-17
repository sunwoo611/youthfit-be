from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IngestedAtMixin


class Policy(IngestedAtMixin, Base):
    """온통청년 청년정책 (getPlcy). 필터용 정규화 컬럼 + 원문 `raw`."""

    __tablename__ = "policies"

    plcy_no: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    description: Mapped[str | None] = mapped_column(Text)
    # lclsfNm / mclsfNm 은 콤마로 여러 분류가 붙어 올 수 있다
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    subcategories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    support_content: Mapped[str | None] = mapped_column(Text)

    min_age: Mapped[int | None] = mapped_column(Integer)
    max_age: Mapped[int | None] = mapped_column(Integer)
    age_limited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    marriage_code: Mapped[str | None] = mapped_column(String(10))
    earn_cond_code: Mapped[str | None] = mapped_column(String(10))
    earn_min: Mapped[int | None] = mapped_column(BigInteger)
    earn_max: Mapped[int | None] = mapped_column(BigInteger)
    earn_etc: Mapped[str | None] = mapped_column(Text)
    job_codes: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default="{}"
    )
    school_codes: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default="{}"
    )
    special_codes: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default="{}"
    )
    major_codes: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, server_default="{}"
    )
    # zipCd 의 법정 시군구 코드(5자리) / 앞 2자리에서 파생한 시도 코드
    sigungu_codes: Mapped[list[str]] = mapped_column(
        ARRAY(String(5)), nullable=False, server_default="{}"
    )
    sido_codes: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)), nullable=False, server_default="{}"
    )

    apply_period_code: Mapped[str | None] = mapped_column(String(10))
    apply_start: Mapped[date | None] = mapped_column(Date)
    apply_end: Mapped[date | None] = mapped_column(Date)
    apply_url: Mapped[str | None] = mapped_column(Text)
    ref_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    apply_method: Mapped[str | None] = mapped_column(Text)
    screening_method: Mapped[str | None] = mapped_column(Text)
    documents: Mapped[str | None] = mapped_column(Text)
    extra_qualification: Mapped[str | None] = mapped_column(Text)
    exclusion: Mapped[str | None] = mapped_column(Text)
    org_name: Mapped[str | None] = mapped_column(Text)

    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_policies_categories", "categories", postgresql_using="gin"),
        Index("ix_policies_sido_codes", "sido_codes", postgresql_using="gin"),
        Index("ix_policies_job_codes", "job_codes", postgresql_using="gin"),
        Index("ix_policies_apply_end", "apply_end"),
    )
