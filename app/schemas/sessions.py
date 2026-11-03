"""역질문 세션 요청·응답. 질문 변수와 선택지는 서버가 정한다(LLM 출력 아님)."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


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
