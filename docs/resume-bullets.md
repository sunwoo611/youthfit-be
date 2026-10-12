# 이력서·자소서용 기록 (STAR)

작성 규칙: 새 항목은 맨 아래에 추가. 수치가 없으면 `(측정 전)`으로 표시하고 나중에 채운다.
관련 근거 문서(ADR/journal)를 반드시 링크한다.

## 프로젝트 소개 (이력서 상단용)

- **청핏(YouthFit)**: 자연어 한 문장 + 3~5회 선택형 역질문으로 청년 정책 지원금과 우대 적금을 통합 추천하는 LLM 서비스. 백엔드·데이터·AI 담당 (FastAPI, PostgreSQL, OpenAI).

## 항목

### 1. 환각·외부 API 리스크를 고려한 아키텍처 설계

- **Situation**: 공공 API 호출 제한·지연, LLM의 허위 자격 조건/URL 생성 위험이 있는 추천 서비스.
- **Task**: 응답 지연·비용·신뢰성을 동시에 통제하는 구조 설계.
- **Action**: 일 배치로 PostgreSQL에 미러링하고, SQL 1차 필터로 후보를 축소한 뒤 LLM은 역질문/근거 서술에만 사용하도록 설계. 추천 URL·자격 조건은 DB 값만 바인딩. 별도 Vector DB 대신 PostgreSQL 중심(필요 시 pgvector)으로 MVP 범위 통제.
- **Result**: (측정 전) 외부 API 의존 제거, LLM 컨텍스트 축소.
- 근거: [ADR 001](decisions/001-stack-and-mvp-scope.md)

### 2. 문서 간 불일치 정리 및 개발 착수 체계화

- **Situation**: LLM으로 순차 생성한 계획서와 발표자료의 기술 표현(Vector DB)이 불일치.
- **Action**: 두 문서를 대조해 구현 기준을 정하고 ADR로 기록, DB 미연결 상태에서도 기동되는 FastAPI 골격과 테스트를 먼저 구축.
- **Result**: 이후 기술 결정을 ADR/journal로 누적 가능한 구조 마련.
- 근거: [journal 2026-10-12](journal/2026-10-12-project-bootstrap.md)
