"""질문 문구화 단위 테스트. 세션 API 연결은 별도."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from app.llm.client import LlmError, Usage
from app.schemas.sessions import Question, QuestionOption, QuestionVariable
from app.services import question_phrasing as svc
from app.services.question_phrasing import LlmPhrasedQuestion, LlmPhrasedQuestions

USAGE = Usage(model="test", input_tokens=1, output_tokens=1, latency_ms=1)


def _question(
    qid: str = "q1",
    *,
    variable: QuestionVariable = QuestionVariable.MARRIAGE,
    text: str = "결혼하셨나요?",
) -> Question:
    return Question(
        id=qid,
        variable=variable,
        text=text,
        type="single",
        options=[
            QuestionOption(value="single", label="미혼"),
            QuestionOption(value="married", label="기혼"),
        ],
        affected=10,
    )


class FakeLlm:
    def __init__(
        self,
        items: list[LlmPhrasedQuestion] | None = None,
        *,
        error: bool = False,
    ) -> None:
        self.items = items or []
        self.error = error
        self.prompts: list[str] = []

    async def parse(self, system: str, user: str, schema: type[BaseModel]) -> tuple[Any, Usage]:
        self.prompts.append(user)
        if self.error:
            raise LlmError("LLM 호출 시간 초과 (15초)")
        assert schema is LlmPhrasedQuestions
        return LlmPhrasedQuestions(items=self.items), USAGE


def test_build_prompt_omits_options_and_keeps_template_fields():
    questions = [
        _question("q1"),
        _question(
            "q2",
            variable=QuestionVariable.SCHOOL,
            text="학력은 어떻게 되나요? (재학·졸업 예정 포함)",
        ),
    ]
    prompt = svc.build_prompt("서울 취준생", questions)
    assert "서울 취준생" in prompt
    assert '"id": "q1"' in prompt or '"id":"q1"' in prompt
    assert "marriage" in prompt
    assert "결혼하셨나요?" in prompt
    assert "single" not in prompt
    assert "미혼" not in prompt
    assert "affected" not in prompt


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  결혼  하셨나요?  ", "결혼 하셨나요?"),
        ("혼인 여부 https://youth.seoul.go.kr 알려주세요?", "혼인 여부 알려주세요?"),
        ("youthcenter.go.kr에서 학력을 알려주세요?", "에서 학력을 알려주세요?"),
    ],
)
def test_clean_text_removes_urls_and_collapses_spaces(text: str, expected: str):
    assert svc.clean_text(text) == expected


def test_clean_text_truncates():
    cleaned = svc.clean_text("가" * 100, 10)
    assert len(cleaned) == 10
    assert cleaned.endswith("…")


def test_merge_texts_keeps_template_for_missing_empty_unknown_ids():
    questions = [
        _question("q1"),
        _question("q2", variable=QuestionVariable.INCOME_BUCKET, text="소득?"),
    ]
    merged = svc.merge_texts(
        questions,
        [
            LlmPhrasedQuestion(id="q1", text="혼인 상태를 알려 주시겠어요?"),
            LlmPhrasedQuestion(id="q9", text="이상한 질문"),
            LlmPhrasedQuestion(id="q2", text="   "),
            LlmPhrasedQuestion(id="q1", text="중복은 무시"),
        ],
    )
    assert merged[0].text == "혼인 상태를 알려 주시겠어요?"
    assert merged[0].options == questions[0].options
    assert merged[0].variable is QuestionVariable.MARRIAGE
    assert merged[1].text == "소득?"


def test_merge_texts_falls_back_when_date_mentioned():
    questions = [_question("q1", text="결혼하셨나요?")]
    merged = svc.merge_texts(
        questions,
        [LlmPhrasedQuestion(id="q1", text="2024년 마감 전에 혼인 상태를 알려 주세요?")],
    )
    assert merged[0].text == "결혼하셨나요?"


def test_merge_texts_keeps_text_after_url_stripped():
    questions = [_question("q1", text="결혼하셨나요?")]
    merged = svc.merge_texts(
        questions,
        [LlmPhrasedQuestion(id="q1", text="혼인 여부(https://example.com)를 알려 주세요?")],
    )
    assert merged[0].text == "혼인 여부를 알려 주세요?"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_llm",
    [lambda: None, lambda: FakeLlm(error=True)],
    ids=["no-key", "llm-error"],
)
async def test_phrase_questions_falls_back_to_template(
    make_llm: Callable[[], FakeLlm | None],
):
    questions = [_question()]
    result = await svc.phrase_questions(make_llm(), "서울 취준생", questions)
    assert result[0].text == questions[0].text
    assert result[0].options == questions[0].options


@pytest.mark.asyncio
async def test_phrase_questions_applies_llm_text_only():
    questions = [_question("q1"), _question("q2", variable=QuestionVariable.SCHOOL, text="학력?")]
    llm = FakeLlm(
        [
            LlmPhrasedQuestion(id="q2", text="학력은 어느 단계인가요?"),
            LlmPhrasedQuestion(id="q1", text="결혼 여부를 알려 주세요?"),
        ]
    )
    result = await svc.phrase_questions(llm, "경기 직장인", questions)
    assert [q.text for q in result] == ["결혼 여부를 알려 주세요?", "학력은 어느 단계인가요?"]
    assert result[0].id == "q1"
    assert result[0].options[0].value == "single"
    assert "경기 직장인" in llm.prompts[0]


@pytest.mark.asyncio
async def test_phrase_questions_empty_input_skips_llm():
    llm = FakeLlm([LlmPhrasedQuestion(id="q1", text="무시")])
    assert await svc.phrase_questions(llm, "쿼리", []) == []
    assert llm.prompts == []
