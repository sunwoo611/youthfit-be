"""POST /v1/candidates 요청·응답. URL·자격 근거는 모두 DB 값에서 만든다."""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.ingest import codes
from app.services.policy_status import Availability


class EmploymentStatus(StrEnum):
    EMPLOYED = "employed"
    SELF_EMPLOYED = "self_employed"
    UNEMPLOYED = "unemployed"
    FREELANCER = "freelancer"
    DAY_LABORER = "day_laborer"
    FOUNDER = "founder"
    SHORT_TERM = "short_term"
    FARMER = "farmer"


class Interest(StrEnum):
    HOUSING = "housing"
    JOBS = "jobs"
    FINANCE = "finance"
    EDUCATION = "education"
    WELFARE = "welfare"
    PARTICIPATION = "participation"


class LinkSource(StrEnum):
    APPLY = "apply"
    REFERENCE = "reference"
    ONTONG_DETAIL = "ontong_detail"
    COMPANY_HOMEPAGE = "company_homepage"


class CandidateRequest(BaseModel):
    age: int | None = Field(default=None, ge=0, le=120)
    sido_code: str | None = Field(
        default=None, description="법정 시도 코드 2자리 (예: 11 서울, 41 경기). 없으면 전국 정책만."
    )
    employment_status: EmploymentStatus | None = None
    interests: list[Interest] = Field(default_factory=list, max_length=len(Interest))
    query: str | None = Field(
        default=None,
        max_length=500,
        description="자연어 한 문장. 위 필드가 비어 있을 때만 규칙 사전으로 보조 추출한다.",
    )

    @field_validator("sido_code")
    @classmethod
    def _known_sido(cls, value: str | None) -> str | None:
        if value is not None and value not in codes.SIDO:
            raise ValueError(f"알 수 없는 시도 코드: {value}")
        return value


class AppliedConditions(BaseModel):
    age: int | None
    sido_code: str | None
    employment_status: EmploymentStatus | None
    interests: list[Interest]
    from_query: list[str] = Field(description="query 에서 추출해 채운 필드 이름")


class PolicyEligibility(BaseModel):
    age_limited: bool
    min_age: int | None
    max_age: int | None
    region_scope: Literal["sido", "national", "sigungu"] = Field(
        description="sido: 시도 전체, national: 전국, sigungu: 일부 시군구만(거주지 확인 필요)"
    )
    sido_codes: list[str]
    sigungu_codes: list[str] = Field(description="region_scope 가 sigungu 일 때만 채운다")
    employment: list[str] = Field(description="취업상태 조건. 비어 있으면 제한 없음")


class PolicyCandidate(BaseModel):
    plcy_no: str
    name: str
    org_name: str | None
    categories: list[str]
    availability: Availability
    apply_start: date | None
    apply_end: date | None
    biz_end: date | None
    eligibility: PolicyEligibility
    official_url: str
    url_source: LinkSource


class PolicyCandidates(BaseModel):
    total: int = Field(description="중복 제거 후 조건을 만족한 전체 건수 (상한 적용 전)")
    items: list[PolicyCandidate]


class SavingCandidate(BaseModel):
    fin_co_no: str
    fin_prdt_cd: str
    name: str
    company_name: str | None
    is_youth: bool
    max_rate: float | None = Field(description="옵션 중 최고 우대금리(%)")
    base_rate: float | None = Field(description="옵션 중 최고 기본금리(%)")
    join_member: str | None
    special_condition: str | None
    official_url: str | None
    url_source: LinkSource | None


class SavingCandidates(BaseModel):
    total: int
    items: list[SavingCandidate]


class CandidateResponse(BaseModel):
    as_of: date
    conditions: AppliedConditions
    policies: PolicyCandidates
    savings: SavingCandidates
