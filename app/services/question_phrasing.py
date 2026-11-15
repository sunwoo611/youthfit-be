"""역질문 문구화. 서버가 고른 변수·선택지는 그대로 두고, 질문 text 만 LLM 이 다듬는다.

키 없음·호출 실패·id 불일치·빈 문구면 템플릿 text 를 유지한다.
후보 목록은 프롬프트에 넣지 않는다. 변수 선정은 question_engine 이 이미 끝냈다.
"""

import json
import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel

from app.llm.client import LlmClient, LlmError
from app.schemas.sessions import Question

logger = logging.getLogger(__name__)

# 칩 UI 한 줄. 템플릿도 이보다 짧다.
TEXT_MAX_CHARS = 80

# 선정 사유(recommendation_service)와 같은 URL·날짜 방어. 질문에도 링크·마감 언급이 섞일 수 있다.
_URL_TAIL = r"[^\s()\[\]{}<>가-힣]*"
_URL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    rf"|https?://{_URL_TAIL}"
    r"|(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)+"
    r"(?:go\.kr|or\.kr|co\.kr|ac\.kr|re\.kr|kr|com|net|org|me|ly|io)(?![A-Za-z0-9])"
    rf"(?:/{_URL_TAIL})?",
    re.IGNORECASE,
)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
_SPACES = re.compile(r"\s+")
_DATE = re.compile(
    r"20\d{2}\s*(?:년|[.\-/]\s*\d{1,2})|\d{1,2}\s*월\s*\d{1,2}\s*일|마감|신청\s*기간|접수\s*기간"
)

SYSTEM_PROMPT = """\
너는 청년 정책 추천 서비스의 역질문 문구 작성자다. 사용자가 칩으로 고를 질문 문장만 다듬는다.

규칙:
- 입력 JSON의 questions 항목마다 id 를 그대로 쓰고, text 만 짧은 구어체 한국어로 고친다.
- id·variable·선택지(value·label)를 바꾸거나 질문을 추가·삭제하지 않는다.
- 자격 조건, 숫자, 지역명, 지원 금액을 새로 만들지 않는다. 입력 text 에 있는 정보만 쓴다.
- 링크, URL, 웹사이트 주소, 신청 기간, 마감일은 쓰지 않는다.
- text 는 한 문장, 80자 이내, 물음표로 끝낸다.
- query 는 말투만 참고하고, query 에 없는 조건을 질문으로 끌어오지 않는다.
"""


class LlmPhrasedQuestion(BaseModel):
    """LLM 출력 1건. 길이 제한은 서버가 자른다."""

    id: str
    text: str


class LlmPhrasedQuestions(BaseModel):
    items: list[LlmPhrasedQuestion]


def build_prompt(query: str, questions: Sequence[Question]) -> str:
    """후보·선택지는 빼고 id·variable·템플릿 text 만 넣는다."""
    payload = {
        "query": query,
        "questions": [
            {"id": question.id, "variable": question.variable, "text": question.text}
            for question in questions
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def clean_text(text: str | None, limit: int = TEXT_MAX_CHARS) -> str:
    """URL 을 지우고 공백을 하나로 모은 뒤 limit 글자로 자른다."""
    text = _SPACES.sub(" ", _EMPTY_BRACKETS.sub("", _URL.sub("", text or ""))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def merge_texts(
    questions: Sequence[Question], items: Sequence[LlmPhrasedQuestion]
) -> list[Question]:
    """입력에 없는 id·중복은 버리고, 비었거나 날짜 언급·빠진 질문은 템플릿을 유지한다."""
    wanted = {question.id for question in questions}
    by_id: dict[str, LlmPhrasedQuestion] = {}
    for item in items:
        if item.id in wanted and item.id not in by_id:
            by_id[item.id] = item
    dropped = len(items) - len(by_id)
    if dropped:
        logger.info("LLM 질문 문구 %d건을 입력에 없거나 중복이라 버림", dropped)

    merged: list[Question] = []
    for question in questions:
        item = by_id.get(question.id)
        text = clean_text(item.text) if item else ""
        if item is None or not text or _DATE.search(text):
            merged.append(question)
            continue
        merged.append(question.model_copy(update={"text": text}))
    return merged


async def phrase_questions(
    llm: LlmClient | None, query: str, questions: Sequence[Question]
) -> list[Question]:
    """(최종 질문 목록). 실패·키 없음이면 템플릿 그대로."""
    if llm is None or not questions:
        return list(questions)
    try:
        output, _ = await llm.parse(
            SYSTEM_PROMPT, build_prompt(query, questions), LlmPhrasedQuestions
        )
    except LlmError as exc:
        logger.warning("질문 문구 LLM 실패, 템플릿 유지: %s", exc)
        return list(questions)
    return merge_texts(questions, output.items)
