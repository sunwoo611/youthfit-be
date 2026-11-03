"""역질문 세션 요청·응답. 질문 변수와 선택지는 서버가 정한다(LLM 출력 아님)."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.schemas.candidates import CandidateRequest, CandidateResponse


class QuestionVariable(StrEnum):
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
    affected: int = Field(description="답에 따라 남거나 빠지는 후보 수")


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
