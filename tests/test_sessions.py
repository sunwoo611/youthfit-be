"""역질문 세션. 답 검증은 순수 함수로, 생성·답변·조회 흐름은 롤백 트랜잭션에서 검증한다."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_db_connection, get_db_connector, get_db_transaction, get_today
from app.api.v1.sessions import ANSWER_LIMIT, CREATE_LIMIT
from app.core.database import ConnectionFactory
from app.ingest import sigungu
from app.main import app
from app.models import QuestionSession
from app.schemas.candidates import AppliedConditions, Marriage, School
from app.schemas.sessions import (
    Answer,
    AnswerRequest,
    CachedReason,
    Question,
    QuestionVariable,
)
from app.services.question_engine import REGISTRY
from app.services.session_service import InvalidAnswerError, apply_answers, delete_old_sessions
from tests.test_candidates import FUTURE, TODAY, _insert_policies, _policy

QUERY = "서울 사는 25살 취준생"
SESSIONS = QuestionSession.__table__


def _question(qid: str, variable: QuestionVariable) -> Question:
    base = AppliedConditions(
        age=None, sido_code="11", employment_status=None, interests=[], from_query=[]
    )
    return Question(
        id=qid,
        variable=variable,
        text="",
        type="single",
        options=REGISTRY[variable].options(base),
        affected=1,
    )


QUESTIONS = [_question("q1", QuestionVariable.SCHOOL), _question("q2", QuestionVariable.MARRIAGE)]
BASE = AppliedConditions(
    age=25, sido_code="11", employment_status=None, interests=[], from_query=[]
)


# --- 답 검증 ---


def test_apply_answers_sets_answered_fields_only():
    conditions = apply_answers(BASE, QUESTIONS, [Answer(question_id="q2", values=["single"])])
    assert conditions.marriage is Marriage.SINGLE
    assert conditions.school is None
    assert apply_answers(BASE, QUESTIONS, []) == BASE


@pytest.mark.parametrize(
    "answer",
    [
        Answer(question_id="q9", values=["single"]),
        Answer(question_id="q2", values=["divorced"]),
        Answer(question_id="q2", values=["college_enrolled"]),
        Answer(question_id="q2", values=["single", "married"]),
    ],
)
def test_apply_answers_rejects_answers_outside_stored_questions(answer: Answer):
    with pytest.raises(InvalidAnswerError):
        apply_answers(BASE, QUESTIONS, [answer])


def test_answer_request_rejects_duplicate_or_empty_answers():
    with pytest.raises(ValidationError):
        AnswerRequest(
            answers=[
                Answer(question_id="q1", values=["graduate_school"]),
                Answer(question_id="q1", values=["college_enrolled"]),
            ]
        )
    with pytest.raises(ValidationError):
        Answer(question_id="q1", values=[])


def test_cached_reason_allows_at_most_three_check_points():
    reason = {"plcy_no": "P1", "reason": "사유", "reason_source": "template"}
    assert CachedReason(**reason, check_points=["a", "b", "c"]).check_points == ["a", "b", "c"]
    with pytest.raises(ValidationError):
        CachedReason(**reason, check_points=["a", "b", "c", "d"])


# --- 쓰기 트랜잭션 ---


class _FakeConnection:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.events: list[str] = []
        self.fail_commit = fail_commit

    def begin(self) -> "_FakeConnection":
        return self

    async def __aenter__(self) -> "_FakeConnection":
        self.events.append("begin")
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        if exc_type is not None:
            self.events.append("rollback")
            return
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.events.append("commit")


def _transaction_app(conn: _FakeConnection) -> FastAPI:
    mini = FastAPI()

    @mini.post("/ok")
    async def ok(_: Annotated[object, Depends(get_db_transaction, scope="function")]) -> None:
        return None

    @mini.post("/fail")
    async def fail(_: Annotated[object, Depends(get_db_transaction, scope="function")]) -> None:
        raise HTTPException(422, "bad")

    async def _conn() -> AsyncIterator[_FakeConnection]:
        yield conn

    mini.dependency_overrides[get_db_connection] = _conn
    return mini


async def _post(conn: _FakeConnection, path: str) -> int:
    transport = ASGITransport(app=_transaction_app(conn), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return (await c.post(path)).status_code


async def test_write_transaction_commits_or_rolls_back():
    ok, fail = _FakeConnection(), _FakeConnection()
    assert await _post(ok, "/ok") == 200
    assert ok.events == ["begin", "commit"]
    assert await _post(fail, "/fail") == 422
    assert fail.events == ["begin", "rollback"]


async def test_write_transaction_commit_failure_is_not_reported_as_success():
    assert await _post(_FakeConnection(fail_commit=True), "/ok") == 500


# --- DB 통합 ---


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


def _seoul_wide(plcy_no: str, **overrides: object) -> dict:
    """서울 시군구를 모두 나열한 시도 전체 정책. 시군구 질문이 생기지 않는다."""
    return _policy(plcy_no, sigungu_codes=list(sigungu.SIGUNGU["11"]), **overrides)


async def _insert_mixed(conn: AsyncConnection) -> None:
    """제한 없음 20, 기혼 전용 5, 대학 재학 전용 5. 학력(6/8 선택지) > 혼인(1/2) 순으로 묻는다."""
    # 한 번에 넣으면 첫 행의 컬럼만 쓰이므로 컬럼 구성이 다른 묶음은 따로 넣는다.
    await _insert_policies(conn, *(_seoul_wide(f"P-{i:02d}") for i in range(20)))
    await _insert_policies(
        conn, *(_seoul_wide(f"MARRIED-{i}", marriage_code="0055001") for i in range(5))
    )
    await _insert_policies(
        conn, *(_seoul_wide(f"COLLEGE-{i}", school_codes=["0049005"]) for i in range(5))
    )


async def _create(client: AsyncClient) -> dict[str, Any]:
    res = await client.post("/v1/sessions", json={"query": QUERY})
    assert res.status_code == 201, res.text
    return res.json()


async def _answer(client: AsyncClient, session_id: str, answers: dict[str, str]):
    return await client.post(
        f"/v1/sessions/{session_id}/answers",
        json={"answers": [{"question_id": q, "values": [v]} for q, v in answers.items()]},
    )


async def _stored(conn: AsyncConnection, session_id: str) -> dict[str, Any]:
    result = await conn.execute(select(SESSIONS).where(SESSIONS.c.id == uuid.UUID(session_id)))
    return dict(result.mappings().one())


def _plcy_nos(body: dict[str, Any]) -> set[str]:
    return {item["plcy_no"] for item in body["candidates"]["policies"]["items"]}


async def test_create_session_stores_conditions_questions_and_initial_ids(
    client: AsyncClient, db_conn: AsyncConnection
):
    await _insert_mixed(db_conn)

    body = await _create(client)

    assert [(q["id"], q["variable"], q["affected"]) for q in body["questions"]] == [
        ("q1", "school", 5),
        ("q2", "marriage", 5),
    ]
    assert body["query"] == QUERY
    assert body["answers"] == []
    assert body["candidates"]["as_of"] == TODAY.isoformat()
    assert body["candidates"]["conditions"]["sido_code"] == "11"
    assert body["candidates"]["policies"]["total"] == 30

    stored = await _stored(db_conn, body["session_id"])
    assert stored["as_of"] == TODAY
    assert stored["query"] == QUERY
    assert stored["conditions"] == body["candidates"]["conditions"]
    assert stored["questions"] == body["questions"]
    assert stored["answers"] is None
    assert set(stored["initial_policy_ids"]) == _plcy_nos(body)
    assert stored["final_policy_ids"] == []
    assert stored["answers_revision"] == 0
    assert await _cache_is_sql_null(db_conn, body["session_id"])
    assert stored["expires_at"] - stored["created_at"] == timedelta(hours=24)


async def test_answers_narrow_candidates_and_resubmission_overwrites(
    client: AsyncClient, db_conn: AsyncConnection
):
    await _insert_mixed(db_conn)
    session_id = (await _create(client))["session_id"]

    res = await _answer(client, session_id, {"q1": "high_school_graduated", "q2": "single"})
    assert res.status_code == 200, res.text
    body = res.json()
    conditions = body["candidates"]["conditions"]
    assert (conditions["school"], conditions["marriage"]) == (
        School.HIGH_SCHOOL_GRADUATED,
        Marriage.SINGLE,
    )
    assert body["candidates"]["policies"]["total"] == 20
    assert _plcy_nos(body) == {f"P-{i:02d}" for i in range(20)}
    stored = await _stored(db_conn, session_id)
    assert stored["answers"] == [
        {"question_id": "q1", "values": ["high_school_graduated"]},
        {"question_id": "q2", "values": ["single"]},
    ]
    assert set(stored["final_policy_ids"]) == _plcy_nos(body)

    res = await _answer(client, session_id, {"q2": "married"})
    body = res.json()
    assert body["candidates"]["conditions"]["school"] is None
    assert body["candidates"]["policies"]["total"] == 30
    stored = await _stored(db_conn, session_id)
    assert stored["answers"] == [{"question_id": "q2", "values": ["married"]}]
    assert len(stored["final_policy_ids"]) == 30

    res = await client.get(f"/v1/sessions/{session_id}")
    assert res.status_code == 200
    got = res.json()
    assert got["answers"] == [{"question_id": "q2", "values": ["married"]}]
    assert got["questions"] == body["questions"]
    assert got["candidates"] == body["candidates"]


async def _cache_is_sql_null(conn: AsyncConnection, session_id: str) -> bool:
    """드라이버는 JSON null 도 None 으로 돌려주므로 DB 에서 직접 확인한다."""
    return await conn.scalar(
        select(SESSIONS.c.recommendations.is_(None)).where(SESSIONS.c.id == uuid.UUID(session_id))
    )


async def test_answer_resubmission_clears_cache_and_bumps_revision(
    client: AsyncClient, db_conn: AsyncConnection
):
    await _insert_mixed(db_conn)
    session_id = (await _create(client))["session_id"]
    await _answer(client, session_id, {"q2": "single"})
    assert (await _stored(db_conn, session_id))["answers_revision"] == 1
    cached = CachedReason(
        plcy_no="P-00", reason="서울 거주 청년 대상", check_points=[], reason_source="llm"
    )
    await db_conn.execute(
        update(SESSIONS)
        .where(SESSIONS.c.id == uuid.UUID(session_id))
        .values(recommendations=[cached.model_dump(mode="json")])
    )

    res = await _answer(client, session_id, {"q2": "married"})

    assert res.status_code == 200, res.text
    assert (await _stored(db_conn, session_id))["answers_revision"] == 2
    assert await _cache_is_sql_null(db_conn, session_id)


async def test_questions_are_scored_over_all_candidates_not_top_50(
    client: AsyncClient, db_conn: AsyncConnection
):
    seoul_all = list(sigungu.SIGUNGU["11"])
    await _insert_policies(
        db_conn,
        *(_policy(f"SIDO-{i:02d}", sigungu_codes=seoul_all) for i in range(50)),
        *(
            _policy(f"GANGNAM-{i}", sigungu_codes=["11680"], apply_end=date(2026, 11, 1))
            for i in range(5)
        ),
    )
    body = await _create(client)
    assert [(q["variable"], q["affected"]) for q in body["questions"]] == [("sigungu", 5)]
    assert body["candidates"]["policies"]["total"] == 55
    assert not any(nos.startswith("GANGNAM") for nos in _plcy_nos(body))

    body = (await _answer(client, body["session_id"], {"q1": "11680"})).json()
    items = body["candidates"]["policies"]["items"]
    assert [item["plcy_no"] for item in items[:5]] == [f"GANGNAM-{i}" for i in range(5)]

    body = (await _answer(client, body["session_id"], {"q1": "11110"})).json()
    assert body["candidates"]["policies"]["total"] == 50
    assert not any(nos.startswith("GANGNAM") for nos in _plcy_nos(body))


async def test_requery_uses_session_as_of(client: AsyncClient, db_conn: AsyncConnection):
    await _insert_mixed(db_conn)
    await _insert_policies(db_conn, _seoul_wide("ENDS-TODAY", apply_end=TODAY))
    session_id = (await _create(client))["session_id"]

    app.dependency_overrides[get_today] = lambda: FUTURE
    body = (await _answer(client, session_id, {"q2": "single"})).json()
    assert body["candidates"]["as_of"] == TODAY.isoformat()
    assert "ENDS-TODAY" in _plcy_nos(body)


async def test_invalid_answer_is_422_and_not_stored(client: AsyncClient, db_conn: AsyncConnection):
    await _insert_mixed(db_conn)
    session_id = (await _create(client))["session_id"]

    res = await _answer(client, session_id, {"q2": "divorced"})
    assert res.status_code == 422
    assert "divorced" in res.json()["detail"]
    duplicate = {
        "answers": [
            {"question_id": "q2", "values": ["single"]},
            {"question_id": "q2", "values": ["married"]},
        ]
    }
    res = await client.post(f"/v1/sessions/{session_id}/answers", json=duplicate)
    assert res.status_code == 422
    assert (await _stored(db_conn, session_id))["answers"] is None


async def test_missing_or_expired_session_is_404(client: AsyncClient, db_conn: AsyncConnection):
    await _insert_mixed(db_conn)
    missing = str(uuid.uuid4())
    assert (await client.get(f"/v1/sessions/{missing}")).status_code == 404
    assert (await _answer(client, missing, {"q2": "single"})).status_code == 404

    session_id = (await _create(client))["session_id"]
    await db_conn.execute(
        update(SESSIONS).where(SESSIONS.c.id == uuid.UUID(session_id)).values(expires_at=func.now())
    )
    assert (await client.get(f"/v1/sessions/{session_id}")).status_code == 404
    assert (await _answer(client, session_id, {"q2": "single"})).status_code == 404


async def test_delete_old_sessions_keeps_recent_ones(client: AsyncClient, db_conn: AsyncConnection):
    ids = [(await _create(client))["session_id"] for _ in range(3)]
    for session_id, age in zip(ids, (31, 29, 0), strict=True):
        await db_conn.execute(
            update(SESSIONS)
            .where(SESSIONS.c.id == uuid.UUID(session_id))
            .values(created_at=func.now() - timedelta(days=age))
        )

    assert await delete_old_sessions(db_conn, timedelta(days=30)) == 1
    remaining = (await db_conn.execute(select(SESSIONS.c.id))).scalars().all()
    assert {str(session_id) for session_id in remaining} == set(ids[1:])


@pytest.mark.parametrize("payload", [{}, {"query": "   "}, {"query": QUERY, "sido_code": "99"}])
async def test_create_session_rejects_invalid_request(client: AsyncClient, payload: dict[str, Any]):
    assert (await client.post("/v1/sessions", json=payload)).status_code == 422
