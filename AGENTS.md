# AGENTS.md — YouthFit Backend

## 프로젝트

청핏(YouthFit): 자연어 한 문장 → LLM 역질문(3~5개 칩) → 청년 정책 지원금 추천(근거·공식 URL). 적금은 제외(`docs/decisions/010-policy-only-scope.md`).
이 저장소는 백엔드(FastAPI + PostgreSQL). 프론트엔드는 별도(Next.js).

## 아키텍처 요약

1. 일 1회 배치로 온통청년 API → PostgreSQL 미러링 (요청 시 외부 호출 없음)
2. 자연어 → 1차 키워드 → SQL 룰 필터로 정책 후보군(30~50)
3. 후보를 가르는 자격 변수(서버) → 역질문 JSON; `text`만 LLM 문구화(실패 시 템플릿)
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
3차 완료: 역질문 세션(`/v1/sessions`, 기대 제거율 변수·템플릿 질문·답변 재필터, `docs/decisions/008-question-variable-selection.md`), LLM 질문 문구화(`app/services/question_phrasing.py`, 세션 생성 시 `text`만·템플릿 폴백, `docs/decisions/012-llm-question-phrasing.md`), LLM 선정 사유(`POST /v1/sessions/{id}/recommendations`, `docs/decisions/011-llm-recommendation-reason.md`, A~D 실측 `docs/journal/2026-11-13-llm-reason.md`).
적금 제거 완료(2026-11-13): `docs/decisions/010-policy-only-scope.md`.
가짜 전국 필터·시도 고정 역질문(2026-11-20): `docs/decisions/013-fake-national-and-sido-question.md`.
테스트는 키 없이 가짜 LLM으로 돈다.
프롬프트 경량화 완료(2026-11-24): 선정 사유 입력 토큰 -10.5%, 측정 `scripts/measure_reason_prompt.py`, `docs/decisions/015-reason-prompt-slimming.md`, `docs/journal/2026-11-24-prompt-slimming.md`.
다음: FE 연동(9주차).
