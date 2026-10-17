# 2026-10-12 백엔드 저장소 부트스트랩

## 상황

- 계획서 기준 3주차 말인데 `youthfit-be`가 빈 Git 저장소였다.
- 계획서(FastAPI/PostgreSQL)와 발표자료(Vector DB 언급)의 표현이 달라 기술 방향 정리가 필요했다.

## 해결

- 두 PDF를 대조해 "계획서 = 구현 기준, 발표자료 = 설명용"으로 정리하고 ADR 001로 기록했다.
- 우선순위를 `골격 → 수집/스키마 → 룰 필터 → 역질문/RAG`로 잡았다. 데이터가 없으면 필터·LLM이 검증 불가능하기 때문이다.
- 1차 작업은 골격(`/health`, Docker PostgreSQL, uv)과 docs·Cursor 규칙으로 제한했다.
- `DATABASE_URL` 미설정이어도 서버가 뜨고 `/health`가 `degraded`로 응답하도록 해, DB 없이도 FE 연동·테스트가 가능하게 했다.

## 결과 (수치)

- `uv run pytest` 1건 통과, `ruff check` 통과.

## 배운 점

- LLM으로 생성한 문서 간 불일치는 ADR로 "의도적 결정"으로 정리하면 이후 설명이 쉬워진다.

## 이력서 반영 여부

- [x] `resume-bullets.md` 에 추가함
