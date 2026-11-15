"""질문 문구화. 단위는 폴백·id 대조, 세션 생성은 FakeLlm 로 답변 value 기준을 확인한다."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import (
    get_db_connection,
    get_db_connector,
    get_db_transaction,
    get_llm_client,
    get_today,
)
from app.api.v1.sessions import ANSWER_LIMIT, CREATE_LIMIT
from app.core.database import ConnectionFactory
from app.llm.client import LlmError, Usage
from app.main import app
from app.schemas.candidates import AppliedConditions, Marriage
from app.schemas.sessions import Answer, Question, QuestionOption, QuestionVariable
from app.services import question_phrasing as svc
from app.services.question_phrasing import LlmPhrasedQuestion, LlmPhrasedQuestions
from app.services.session_service import apply_answers
from tests.test_candidates import TODAY
from tests.test_sessions import QUERY, _insert_mixed

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
        echo: bool = False,
        prefix: str = "다듬은 ",
    ) -> None:
        self.items = items or []
        self.error = error
        self.echo = echo
        self.prefix = prefix
        self.prompts: list[str] = []

    async def parse(self, system: str, user: str, schema: type[BaseModel]) -> tuple[Any, Usage]:
        self.prompts.append(user)
        if self.error:
            raise LlmError("LLM 호출 시간 초과 (15초)")
        assert schema is LlmPhrasedQuestions
        if self.echo:
            payload = json.loads(user)
            items = [
                LlmPhrasedQuestion(id=question["id"], text=f"{self.prefix}{question['variable']}?")
                for question in payload["questions"]
            ]
            return LlmPhrasedQuestions(items=items), USAGE
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


@pytest.mark.asyncio
async def test_phrased_text_does_not_affect_answer_validation():
    """답은 option.value 로만 검증한다. text 가 바뀌어도 선택지·변수는 그대로다."""
    template = _question("q1", text="결혼하셨나요?")
    llm = FakeLlm([LlmPhrasedQuestion(id="q1", text="혼인 상태를 알려 주시겠어요?")])
    phrased = await svc.phrase_questions(llm, "서울 취준생", [template])

    assert phrased[0].text == "혼인 상태를 알려 주시겠어요?"
    assert phrased[0].options == template.options
    assert phrased[0].variable is QuestionVariable.MARRIAGE

    base = AppliedConditions(
        age=25, sido_code="11", employment_status=None, interests=[], from_query=[]
    )
    conditions = apply_answers(base, phrased, [Answer(question_id="q1", values=["single"])])
    assert conditions.marriage is Marriage.SINGLE


# --- 세션 생성 통합 (FakeLlm) ---


def _connect(conn: AsyncConnection) -> ConnectionFactory:
    @asynccontextmanager
    async def connect() -> AsyncIterator[AsyncConnection]:
        yield conn

    return connect


@pytest.fixture
async def client(db_conn: AsyncConnection) -> AsyncIterator[AsyncClient]:
    async def _conn() -> AsyncIterator[AsyncConnection]:
        yield db_conn

    app.dependency_overrides[get_db_connection] = _conn
    app.dependency_overrides[get_db_transaction] = _conn
    app.dependency_overrides[get_db_connector] = lambda: _connect(db_conn)
    app.dependency_overrides[get_today] = lambda: TODAY
    for limit in (CREATE_LIMIT, ANSWER_LIMIT):
        app.dependency_overrides[limit] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_llm",
    [lambda: None, lambda: FakeLlm(error=True)],
    ids=["no-key", "llm-error"],
)
async def test_create_session_keeps_template_text_when_llm_unusable(
    client: AsyncClient, db_conn: AsyncConnection, make_llm: Callable[[], FakeLlm | None]
):
    await _insert_mixed(db_conn)
    baseline = await client.post("/v1/sessions", json={"query": QUERY})
    assert baseline.status_code == 201, baseline.text
    template_texts = [q["text"] for q in baseline.json()["questions"]]

    llm = make_llm()
    app.dependency_overrides[get_llm_client] = lambda: llm
    res = await client.post("/v1/sessions", json={"query": QUERY})
    assert res.status_code == 201, res.text
    assert [q["text"] for q in res.json()["questions"]] == template_texts
    if llm is not None:
        assert llm.prompts  # 호출은 했지만 실패 → 템플릿 유지


@pytest.mark.asyncio
async def test_create_session_applies_phrased_text_and_ignores_unknown_ids(
    client: AsyncClient, db_conn: AsyncConnection
):
    await _insert_mixed(db_conn)
    baseline = await client.post("/v1/sessions", json={"query": QUERY})
    assert baseline.status_code == 201, baseline.text
    questions = baseline.json()["questions"]
    first_id = questions[0]["id"]
    template_second = questions[1]["text"]
    option_values = {opt["value"] for opt in questions[0]["options"]}

    llm = FakeLlm(
        [
            LlmPhrasedQuestion(id=first_id, text="학력은 어느 단계인가요?"),
            LlmPhrasedQuestion(id="q-missing", text="없는 질문은 버려야 해요?"),
        ]
    )
    app.dependency_overrides[get_llm_client] = lambda: llm

    res = await client.post("/v1/sessions", json={"query": QUERY})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["questions"][0]["text"] == "학력은 어느 단계인가요?"
    assert body["questions"][0]["variable"] == questions[0]["variable"]
    assert {opt["value"] for opt in body["questions"][0]["options"]} == option_values
    assert body["questions"][1]["text"] == template_second
    assert "없는 질문은" not in json.dumps(body["questions"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_create_session_answers_use_option_values_not_phrased_text(
    client: AsyncClient, db_conn: AsyncConnection
):
    await _insert_mixed(db_conn)
    llm = FakeLlm(echo=True, prefix="자연스럽게 여쭤볼게요, ")
    app.dependency_overrides[get_llm_client] = lambda: llm

    created = await client.post("/v1/sessions", json={"query": QUERY})
    assert created.status_code == 201, created.text
    body = created.json()
    assert all(q["text"].startswith("자연스럽게 여쭤볼게요, ") for q in body["questions"])
    marriage = next(q for q in body["questions"] if q["variable"] == "marriage")
    assert "single" in {opt["value"] for opt in marriage["options"]}

    answered = await client.post(
        f"/v1/sessions/{body['session_id']}/answers",
        json={"answers": [{"question_id": marriage["id"], "values": ["single"]}]},
    )
    assert answered.status_code == 200, answered.text
    conditions = answered.json()["candidates"]["conditions"]
    assert conditions["marriage"] == Marriage.SINGLE
    # 문구가 바뀌었어도 잘못된 value 는 거절한다.
    bad = await client.post(
        f"/v1/sessions/{body['session_id']}/answers",
        json={
            "answers": [
                {"question_id": marriage["id"], "values": [marriage["text"]]},
            ]
        },
    )
    assert bad.status_code == 422
