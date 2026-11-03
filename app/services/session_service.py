"""역질문 세션. 1차 후보로 질문을 만들어 저장하고, 답을 조건에 반영해 후보를 다시 고른다.

답은 저장된 질문의 선택지와 대조한 값만 받는다. 재조회 기준일은 세션 생성일(`as_of`)이다.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models import QuestionSession
from app.schemas.candidates import AppliedConditions, CandidateResponse
from app.schemas.sessions import (
    Answer,
    AnswerRequest,
    Question,
    SessionCreateRequest,
    SessionResponse,
)
from app.services.candidate_service import (
    build_response,
    fetch_policy_rows,
    resolve_conditions,
    search_candidates,
)
from app.services.question_engine import apply_answer, select_questions

SESSIONS = QuestionSession.__table__


class SessionNotFoundError(LookupError):
    """없거나 만료된 세션."""


class InvalidAnswerError(ValueError):
    """저장된 질문·선택지와 맞지 않는 답."""


def apply_answers(
    conditions: AppliedConditions, questions: Sequence[Question], answers: Sequence[Answer]
) -> AppliedConditions:
    by_id = {question.id: question for question in questions}
    for answer in answers:
        question = by_id.get(answer.question_id)
        if question is None:
            raise InvalidAnswerError(f"없는 질문: {answer.question_id}")
        # 질문 엔진은 단일 선택 질문만 만든다.
        if len(answer.values) != 1:
            raise InvalidAnswerError(f"{answer.question_id}: 선택지를 하나만 고른다")
        value = answer.values[0]
        if value not in {option.value for option in question.options}:
            raise InvalidAnswerError(f"{answer.question_id}: 선택지에 없는 값 {value}")
        conditions = apply_answer(conditions, question.variable, value)
    return conditions


def _policy_ids(candidates: CandidateResponse) -> list[str]:
    return [item.plcy_no for item in candidates.policies.items]


async def create_session(
    conn: AsyncConnection, request: SessionCreateRequest, today: date
) -> SessionResponse:
    conditions = resolve_conditions(request)
    # 일부 시군구 정책은 정렬상 상위 50 밖에 몰리므로 질문은 전체 후보로 고른다.
    rows = await fetch_policy_rows(conn, conditions, today, limit=None)
    questions = select_questions(rows, conditions)
    candidates = await build_response(conn, conditions, today, rows)
    created = (
        await conn.execute(
            insert(SESSIONS)
            .values(
                as_of=today,
                query=request.query,
                conditions=conditions.model_dump(mode="json"),
                questions=[question.model_dump(mode="json") for question in questions],
                initial_policy_ids=_policy_ids(candidates),
            )
            .returning(SESSIONS.c.id, SESSIONS.c.expires_at)
        )
    ).one()
    return SessionResponse(
        session_id=created.id,
        expires_at=created.expires_at,
        query=request.query,
        questions=questions,
        answers=[],
        candidates=candidates,
    )


async def _load(conn: AsyncConnection, session_id: uuid.UUID) -> Mapping[str, Any]:
    row = (
        (
            await conn.execute(
                select(SESSIONS).where(
                    SESSIONS.c.id == session_id, SESSIONS.c.expires_at > func.now()
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise SessionNotFoundError(str(session_id))
    return row


def _questions(session: Mapping[str, Any]) -> list[Question]:
    return [Question.model_validate(question) for question in session["questions"]]


async def get_session(conn: AsyncConnection, session_id: uuid.UUID) -> SessionResponse:
    session = await _load(conn, session_id)
    questions = _questions(session)
    answers = [Answer.model_validate(answer) for answer in session["answers"] or []]
    initial = AppliedConditions.model_validate(session["conditions"])
    conditions = apply_answers(initial, questions, answers)
    return SessionResponse(
        session_id=session["id"],
        expires_at=session["expires_at"],
        query=session["query"],
        questions=questions,
        answers=answers,
        candidates=await search_candidates(conn, conditions, session["as_of"]),
    )


async def delete_old_sessions(conn: AsyncConnection, retention: timedelta) -> int:
    """생성 후 보관 기간이 지난 세션을 지운다. 만료(24시간)와 달리 분석 데이터까지 없앤다."""
    result = await conn.execute(
        delete(SESSIONS).where(SESSIONS.c.created_at < func.now() - retention)
    )
    return result.rowcount


async def answer_session(
    conn: AsyncConnection, session_id: uuid.UUID, request: AnswerRequest
) -> SessionResponse:
    """답은 1차 조건에 다시 반영하므로, 같은 세션에 다시 내면 이전 답을 덮어쓴다."""
    session = await _load(conn, session_id)
    questions = _questions(session)
    initial = AppliedConditions.model_validate(session["conditions"])
    conditions = apply_answers(initial, questions, request.answers)
    candidates = await search_candidates(conn, conditions, session["as_of"])
    await conn.execute(
        update(SESSIONS)
        .where(SESSIONS.c.id == session_id)
        .values(
            answers=[answer.model_dump(mode="json") for answer in request.answers],
            final_policy_ids=_policy_ids(candidates),
        )
    )
    return SessionResponse(
        session_id=session["id"],
        expires_at=session["expires_at"],
        query=session["query"],
        questions=questions,
        answers=request.answers,
        candidates=candidates,
    )
