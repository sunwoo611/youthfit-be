# AGENTS.md — YouthFit Backend

## 프로젝트

청핏(YouthFit): 자연어 한 문장 → LLM 역질문(3~5개 칩) → 청년 정책 지원금 추천(근거·공식 URL). 적금은 제외(`docs/decisions/010-policy-only-scope.md`).
이 저장소는 백엔드(FastAPI + PostgreSQL). 프론트엔드는 별도(Next.js).

## 아키텍처 요약

1. 일 1회 배치로 온통청년 API → PostgreSQL 미러링 (요청 시 외부 호출 없음)
2. 자연어 → 1차 키워드 → SQL 룰 필터로 정책 후보군(30~50)
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

2차 Build 완료: Alembic 스키마, 온통청년 일 배치 적재, 품질 리포트, 1차 룰 필터 `POST /v1/candidates`.
3차 진행 중: 역질문 세션 LLM 없는 E2E 완료. `/v1/sessions` 생성·조회·답변, `sessions` 테이블, 서버가 기대 제거율로 고른 혼인·학력·소득·시군구 템플릿 질문, 답변 반영 재필터(`docs/decisions/008-question-variable-selection.md`).
적금 제거 완료(2026-11-13): 금감원 적재·적금 테이블·후보 응답 `savings` 삭제(`docs/decisions/010-policy-only-scope.md`).
다음: LLM 선정 사유 서술(최종 후보 상위 5건), LLM 질문 문구화(서버가 고른 변수·선택지는 고정).
