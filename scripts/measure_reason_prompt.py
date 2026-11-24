"""선정 사유 LLM 호출의 입력·출력 토큰과 지연을 대표 사용자 A~D로 잰다 (8주차 경량화 전후 비교용).

실행: uv run python -m scripts.measure_reason_prompt [반복횟수]
실제 DB·LLM 키가 필요하다. 세션은 잰 뒤 삭제한다. 답은 제출하지 않고 첫 후보 기준으로 잰다.
"""

import asyncio
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.api.deps import get_db_connector, get_llm_client
from app.models import QuestionSession
from app.schemas.candidates import EmploymentStatus, Interest
from app.schemas.sessions import ReasonSource, SessionCreateRequest
from app.services import recommendation_service as svc
from app.services.session_service import create_session

KST = timezone(timedelta(hours=9))

USERS = {
    "A 서울 25세 미취업": dict(
        query="서울 사는 25살 취준생이에요",
        age=25,
        sido_code="11",
        employment_status=EmploymentStatus.UNEMPLOYED,
    ),
    "B 경기 29세 재직": dict(
        query="경기도 직장인 29살이에요",
        age=29,
        sido_code="41",
        employment_status=EmploymentStatus.EMPLOYED,
    ),
    "C 서울 27세 주거": dict(
        query="서울 27살 주거 지원이 필요해요",
        age=27,
        sido_code="11",
        interests=[Interest.HOUSING],
    ),
    "D 질의만": dict(query="경기도 사는 31살 직장인인데 월세 지원 받고 싶어요"),
}


class Meter:
    """LLM 호출을 감싸 토큰·지연·프롬프트 길이를 모은다."""

    def __init__(self, inner):
        self.inner = inner
        self.records = []

    async def parse(self, system, user, schema):
        output, usage = await self.inner.parse(system, user, schema)
        self.records.append((usage, len(system) + len(user)))
        return output, usage


async def measure(repeat: int) -> None:
    connect = get_db_connector()
    meter = Meter(get_llm_client())
    today = datetime.now(KST).date()
    rows = []
    for name, fields in USERS.items():
        runs = []
        for _ in range(repeat):
            created = await create_session(connect, None, SessionCreateRequest(**fields), today)
            before = len(meter.records)
            response = await svc.recommend(connect, meter, created.session_id)
            usage, chars = meter.records[before]
            fallback = sum(i.reason_source is ReasonSource.TEMPLATE for i in response.items)
            runs.append(
                (usage.input_tokens, usage.output_tokens, usage.latency_ms, chars, fallback)
            )
            async with connect() as conn:
                await conn.execute(
                    delete(QuestionSession.__table__).where(
                        QuestionSession.__table__.c.id == created.session_id
                    )
                )
        med = [statistics.median(col) for col in zip(*runs, strict=True)]
        rows.append((name, *med))
        print(
            f"{name}: 입력 {med[0]:.0f} / 출력 {med[1]:.0f} / 지연 {med[2]:.0f}ms / "
            f"프롬프트 {med[3]:.0f}자 / 템플릿 대체 {med[4]:.0f}/5  (n={repeat})"
        )
    print(
        json.dumps(
            {
                "입력 평균": statistics.mean(r[1] for r in rows),
                "출력 평균": statistics.mean(r[2] for r in rows),
                "지연 중앙값": statistics.median(r[3] for r in rows),
                "프롬프트자 평균": statistics.mean(r[4] for r in rows),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(measure(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
