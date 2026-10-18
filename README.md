# YouthFit Backend (youthfit-be)

LLM 기반 역질문 대화로 청년 정책·금융상품을 통합 추천하는 서비스 **청핏(YouthFit)** 의 백엔드입니다.
FastAPI + PostgreSQL 기반이며, 프로젝트 배경과 기술 결정 기록은 [`docs/`](docs/README.md)에 있습니다.

## 요구 사항

- [uv](https://docs.astral.sh/uv/)
- Docker (PostgreSQL 실행용)

## 로컬 실행

```powershell
# 1) 의존성 설치
uv sync

# 2) 환경 변수 준비
Copy-Item .env.example .env

# 3) PostgreSQL 기동 + 스키마 마이그레이션
docker compose up -d
uv run alembic upgrade head

# 4) API 서버 실행
uv run uvicorn app.main:app --reload
```

- Swagger UI: http://localhost:8000/docs
- Health check: http://localhost:8000/health
  - `DATABASE_URL` 미설정 → `degraded` / `not_configured`
  - DB 연결 성공 → `ok` / `up`

## 공공 데이터 적재 (일 배치)

온통청년 정책과 금감원 적금(은행·저축은행)을 PostgreSQL 에 미러링합니다. `.env` 에 `ONTONG_YOUTH_API_KEY`, `FSS_API_KEY` 가 필요합니다.

```powershell
uv run python -m app.ingest                  # 전체
uv run python -m app.ingest --source ontong  # 온통청년만
uv run python -m app.ingest --source fss     # 금감원만
```

- 실행 결과(건수, 소요 시간, 미등록 코드)는 표준 출력에, 실행 이력은 `ingest_runs` 테이블에 남습니다.
- 전체 수집이 끝난 경우에만 원본에서 사라진 정책·상품을 삭제합니다. 일부만 수집되면 `partial` 로 기록하고 삭제를 건너뜁니다.
- 실패 시 종료 코드 1, 설정 누락 시 2.

일 1회 스케줄링 예시 (등록은 각자 환경에서):

```powershell
# Windows 작업 스케줄러: 매일 04:00
schtasks /Create /TN "youthfit-ingest" /SC DAILY /ST 04:00 /TR "powershell -NoProfile -Command \"Set-Location D:\project\youthfit\youthfit-be; uv run python -m app.ingest\""
```

```bash
# Linux cron: 매일 04:00
0 4 * * * cd /srv/youthfit-be && uv run python -m app.ingest >> logs/ingest.log 2>&1
```

## 테스트 / 린트

```powershell
uv run pytest
uv run ruff check .
```

## 디렉터리 구조

```
app/
  main.py          # FastAPI 앱 생성
  api/             # 라우터 (엔드포인트)
  core/            # 설정, DB 연결
  ingest/          # 공공 API 수집·정규화·적재 (일 배치 CLI)
  models/          # SQLAlchemy ORM 모델
alembic/           # DB 마이그레이션 (alembic.ini 는 ASCII 만 사용)
tests/
docs/              # 아키텍처, 기술 결정(ADR), 트러블슈팅 일지, 이력서용 기록
compose.yaml       # 로컬 PostgreSQL
```

## 로드맵

| 단계 | 내용 | 상태 |
|------|------|------|
| 1차 | 프로젝트 골격, `/health`, docs·Cursor 규칙 | 완료 |
| 2차 | 스키마(Alembic), 온통청년·금감원 수집, 1차 룰 필터 API | 진행 중 |
| 3차+ | 세션, LLM 역질문, RAG 근거 매칭, 캐싱 | 예정 |
