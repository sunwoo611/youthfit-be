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
- 원본에서 사라진 정책·상품은 두 조건을 모두 만족할 때만 삭제합니다. 하나라도 어긋나면 아무것도 지우지 않고 `partial` 로 기록합니다.
  1. API 가 알려준 전체 건수 이상을 중복 없이 수집했다.
  2. 삭제 대상이 적재 전 기존 행의 5% 이하다 (API 가 일시적으로 적은 건수를 돌려줘도 미러가 비지 않게 하는 상한).
- 실행 상태: `success` / `partial` / `failed` / `cancelled`(Ctrl+C) / `abandoned`(6시간 넘게 종료 기록 없이 남은 실행, 다음 실행 시 정리).
- 실패 시 종료 코드 1, 설정 누락 시 2.

필드 채움률과 크롤링 필요성은 적재된 DB만 조회한다 (쓰기는 하지 않음).

```powershell
uv run python -m app.ingest.report
uv run python -m app.ingest.report --today 2026-10-26
```

일 1회 스케줄링 예시 (등록은 각자 환경에서):

```powershell
# Windows 작업 스케줄러: 매일 04:00
schtasks /Create /TN "youthfit-ingest" /SC DAILY /ST 04:00 /TR "powershell -NoProfile -Command \"Set-Location D:\project\youthfit\youthfit-be; uv run python -m app.ingest\""
```

```bash
# Linux cron: 매일 04:00
0 4 * * * cd /srv/youthfit-be && uv run python -m app.ingest >> logs/ingest.log 2>&1
```

## 후보 추천 API (1차 룰 필터)

`POST /v1/candidates` 는 적재된 DB만 조회해 정책 최대 50건, 적금 최대 20건을 돌려줍니다. 외부 API·LLM 은 호출하지 않습니다.

```powershell
$body = '{"age": 25, "sido_code": "11", "employment_status": "unemployed", "interests": ["housing"]}'
Invoke-RestMethod -Method Post -Uri http://localhost:8000/v1/candidates -ContentType "application/json" -Body $body
```

- 필드는 모두 선택입니다. `query`(자연어 한 문장)를 주면 비어 있는 필드만 규칙 사전으로 채웁니다.
- 시도를 주지 않으면 전국 정책만 고릅니다. 마감 정책은 빼고, 신청 가능 > 신청 예정 > 신청기간 확인 필요 순입니다.
- 카드의 `official_url` 은 DB 값으로만 만듭니다 (신청 URL → 참고 URL → 온통청년 상세 페이지, 적금은 금융회사 홈페이지).

## 개발 서버 배포 (Render)

`main` 에 push 되면 GitHub Actions CI(ruff, pytest)가 돌고, 통과한 커밋만 Render 에 자동 배포됩니다. 컨테이너가 뜰 때 `alembic upgrade head` 를 먼저 실행합니다. 결정 근거는 [ADR 007](docs/decisions/007-dev-deploy-render.md) 에 있습니다.

최초 1회 설정:

1. 이 구성이 `main` 에 merge 되어 있어야 합니다 (`render.yaml`, `.github/workflows/`).
2. Render 대시보드 → **New → Blueprint** → GitHub 레포 연결 → `render.yaml` 선택. `sync: false` 항목(`CORS_ORIGIN_REGEX`, `OPENAI_API_KEY`)은 이때 입력하거나 비워 둡니다.
3. 생성된 `youthfit-db` 의 **External Database URL** 을 복사해 GitHub 레포 **Settings → Secrets and variables → Actions** 에 등록합니다: `DATABASE_URL`, `ONTONG_YOUTH_API_KEY`, `FSS_API_KEY`.
4. GitHub **Actions → Daily ingest → Run workflow** 로 최초 적재를 합니다. 이후 매일 04:00(KST)에 자동 실행됩니다.
5. 프론트 도메인이 정해지면 `render.yaml` 의 `CORS_ORIGINS` 값에 쉼표로 추가해 merge 합니다 (예: `http://localhost:3000,https://youthfit.vercel.app`). 대시보드에서 바꾸면 다음 Blueprint 동기화 때 `render.yaml` 값으로 되돌아갑니다. Vercel 프리뷰 주소는 대시보드에서 `CORS_ORIGIN_REGEX` (예: `https://youthfit-fe-[a-z0-9-]+\.vercel\.app`) 로 허용합니다 (`sync: false` 라 덮어쓰지 않음).

프론트에는 Render 가 발급한 서비스 URL 과 `/docs`(Swagger), `/openapi.json` 을 공유합니다.

- 무료 플랜은 15분 동안 요청이 없으면 슬립하고, 다음 첫 요청이 약 1분 걸립니다.
- 무료 PostgreSQL 은 생성 후 30일이 지나면 만료되고, 14일 유예 뒤 삭제됩니다. 워크스페이스당 무료 DB 는 하나뿐이라 기존 DB 를 지운 뒤 Blueprint 를 다시 동기화해 새로 만듭니다. 외부 URL 이 바뀌므로 GitHub Secret `DATABASE_URL` 을 갱신하고 적재 워크플로를 다시 실행합니다.
- GitHub 레포 **Settings → Branches** 에서 `main` 을 PR merge 만 허용하고 CI 통과를 필수로 두면 공유 서버가 깨질 일이 줄어듭니다.

이미지를 로컬에서 확인하려면:

```powershell
docker build -t youthfit-api:local .
docker run --rm -p 8000:8000 -e DATABASE_URL=postgresql://youthfit:youthfit@host.docker.internal:5432/youthfit youthfit-api:local
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
  schemas/         # 요청·응답 Pydantic 모델
  services/        # 비즈니스 로직 (신청 가능 여부 판정, 후보 필터)
alembic/           # DB 마이그레이션 (alembic.ini 는 ASCII 만 사용)
tests/
docs/              # 아키텍처, 기술 결정(ADR), 트러블슈팅 일지, 이력서용 기록
compose.yaml       # 로컬 PostgreSQL
Dockerfile         # 배포 이미지 (기동 시 마이그레이션 → uvicorn)
render.yaml        # Render Blueprint (웹 서비스 + PostgreSQL)
.github/workflows/ # CI(ruff·pytest), 일 배치 적재
```

## 로드맵

| 단계 | 내용 | 상태 |
|------|------|------|
| 1차 | 프로젝트 골격, `/health`, docs·Cursor 규칙 | 완료 |
| 2차 | 스키마(Alembic), 온통청년·금감원 수집, 1차 룰 필터 API | 완료 |
| 3차+ | 세션, LLM 역질문, RAG 근거 매칭, 캐싱 | 예정 |
