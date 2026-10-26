# AGENTS.md — YouthFit Backend

## 프로젝트

청핏(YouthFit): 자연어 한 문장 → LLM 역질문(3~5개 칩) → 청년 정책 지원금 + 우대 적금 통합 추천(근거·공식 URL).
이 저장소는 백엔드(FastAPI + PostgreSQL). 프론트엔드는 별도(Next.js).

## 아키텍처 요약

1. 일 1회 배치로 온통청년·금감원 API → PostgreSQL 미러링 (요청 시 외부 호출 없음)
2. 자연어 → 1차 키워드 → SQL 룰 필터로 후보군(정책 30~50, 적금 10~20)
3. 후보를 가르는 자격 변수 → LLM이 역질문 JSON 생성
4. 답변으로 후보 확정 → LLM이 DB 메타데이터 기반 선정 사유 서술, URL은 DB 값만 사용

자세한 내용: `docs/architecture.md`, `docs/decisions/`.

## 명령어

```powershell
uv sync
docker compose up -d
uv run uvicorn app.main:app --reload
uv run pytest
uv run ruff check .
```

## 작업 규칙

- 한국어로 응답한다.
- Python은 항상 `uv run`으로 실행한다.
- 기술 결정·트러블슈팅·성과는 `docs/`에 기록한다. 세부 규칙은 `.cursor/rules/youthfit-docs.mdc`.
- 비밀값은 `.env`에만 두고 커밋하지 않는다.
- LLM 출력의 URL·자격 조건은 신뢰하지 않고 DB 값과 대조한다.

## 현재 상태

2차 Build 완료: Alembic 스키마, 온통청년·금감원 일 배치 적재, 품질 리포트, 1차 룰 필터 `POST /v1/candidates`.
다음(3차): 세션, LLM 역질문(후보를 가르는 자격 변수, 시군구 포함), 선정 사유 서술.
