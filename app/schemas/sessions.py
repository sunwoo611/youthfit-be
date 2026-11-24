"""역질문 세션 요청·응답. 질문 변수와 선택지는 서버가 정한다(LLM 출력 아님)."""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.schemas.candidates import CandidateRequest, CandidateResponse, PolicyCandidate


class QuestionVariable(StrEnum):
    SIDO = "sido"
    MARRIAGE = "marriage"
    SCHOOL = "school"
    INCOME_BUCKET = "income_bucket"
    SIGUNGU = "sigungu"


class QuestionOption(BaseModel):
    value: str
    label: str


class Question(BaseModel):
    id: str
    variable: QuestionVariable
    text: str
    type: Literal["single", "multi"]
    options: list[QuestionOption]
    affected: int = Field(
        description=(
            "기대 제거율로 고른 변수에서, 답에 따라 빠질 수 있는 후보 수. "
            "sido 는 제거율 점수 없이 고정 삽입하므로 항상 0(답 후 후보가 늘 수 있음)."
        )
    )


class SessionCreateRequest(CandidateRequest):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ] = Field(
        description="자연어 한 문장. 명시한 필드가 비어 있을 때만 규칙 사전으로 보조 추출한다."
    )


class Answer(BaseModel):
    question_id: str
    values: list[str] = Field(min_length=1, description="고른 선택지 value")


class AnswerRequest(BaseModel):
    answers: list[Answer] = Field(description="답한 질문만 담는다. 다시 내면 이전 답을 덮어쓴다.")

    @model_validator(mode="after")
    def _unique_questions(self) -> Self:
        ids = [answer.question_id for answer in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("같은 질문에 답이 두 번 있다")
        return self


class SessionResponse(BaseModel):
    session_id: uuid.UUID
    expires_at: datetime
    query: str
    questions: list[Question]
    answers: list[Answer] = Field(description="반영된 답. 아직 답하지 않았으면 빈 목록")
    candidates: CandidateResponse = Field(
        description="1차 조건에 답을 반영한 후보. 기준일은 세션 생성일로 고정한다."
    )


class ReasonSource(StrEnum):
    LLM = "llm"
    TEMPLATE = "template"


class RecommendationReason(BaseModel):
    reason: str
    check_points: list[str] = Field(max_length=3, description="사용자가 직접 더 확인할 조건")
    reason_source: ReasonSource = Field(description="LLM 실패·키 없음이면 template")


class CachedReason(RecommendationReason):
    """세션에 캐시하는 정책 1건의 서술. 링크·마감일 같은 DB 값은 담지 않는다."""

    plcy_no: str


class Recommendation(RecommendationReason):
    policy: PolicyCandidate = Field(description="링크·마감일·자격은 이 DB 값만 믿는다")


class RecommendationRequest(BaseModel):
    """이미 보여 준 정책을 빼고 다음 장을 받을 때 쓴다. 생략·빈 목록이면 상위부터."""

    exclude_plcy_nos: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    ] = Field(
        default_factory=list,
        max_length=45,
        description="이미 화면에 쌓인 policy.plcy_no. 최대 45개.",
    )

    @field_validator("exclude_plcy_nos")
    @classmethod
    def _dedupe_exclude(cls, values: list[str]) -> list[str]:
        # 순서를 유지한 채 중복만 제거한다. 빈 문자열은 StringConstraints 가 막는다.
        return list(dict.fromkeys(values))


class RecommendationResponse(BaseModel):
    session_id: uuid.UUID
    as_of: date
    items: list[Recommendation] = Field(description="답을 반영한 최종 후보 상위 정책")
    has_more: bool = Field(
        description="후보 상위 50에서 exclude 를 뺀 뒤 6건 이상이면 true (더 보기 가능)"
    )
