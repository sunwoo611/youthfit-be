# 2026-11-28 Render API 무응답 (FE “서버 안 됨”)

## 상황 / 증상

프론트에서 백엔드가 안 된다고 피드백.  
대상: `https://youthfit-api.onrender.com/health`

- TCP 443은 연결됨
- HTTP는 **120초 타임아웃, 수신 바이트 0** (슬립 깨우기 수준이 아님)
- 로컬: uvicorn 미기동, Docker Desktop 데몬 미기동

## 원인 분석

1. **가설: Render 무료 슬립** → 보통 수십 초 내 응답. 120초·0바이트라 기각.
2. **가설: 무료 Postgres 만료/DB 접속 불가 → 웹 기동 실패**
   - GitHub Secret `DATABASE_URL` 등록일: 2026-10-26
   - 무료 DB 30일 만료 ≈ 2026-11-25
   - 일 배치 ingest: **11-25 success** → **11-26~28 failure**
   - 실패 로그: `SSL/TLS required` / `connection was closed in the middle of operation`
3. Dockerfile `CMD`가 `alembic upgrade head && uvicorn ...` 이라 DB가 안 되면 uvicorn까지 못 감 → 엣지에서 요청이 공회전·타임아웃.

대시보드 확정: `youthfit-db` = **Suspended by Render**, `youthfit-api` = **Failed service**.

## 해결

(진행 중 — DB 재생성부터)

1. 만료된 `youthfit-db` 삭제 (워크스페이스당 무료 DB 1개)
2. Blueprint 동기화로 DB 재생성 → External Database URL 복사 (`sslmode=require` 유지)
3. GitHub Secret `DATABASE_URL` 갱신
4. `youthfit-api` Manual Deploy / Restart (Live + `/health` `database: up`)
5. Actions → Daily ingest → Run workflow 로 정책 재적재

## 결과 (수치)

| 항목 | 값 |
|------|-----|
| `/health` (장애 시) | 120121ms 타임아웃, 0 bytes |
| `/health` (DB 재생성 후) | 200, `database: up` (~528ms) |
| ingest 성공 마지막 | 2026-11-25 (정책 3994건) |
| ingest 11-26~28 schedule | `SSL/TLS required` (만료 DB) |
| ingest 수동 재실행 (DB 복구 후) | 첫 시도: 1페이지 OK 후 `getPlcy` HTTP 400. 재시도 후 성공 |
| 복구 후 스모크 | `/health` ok·up (439ms), `POST /v1/sessions` total=114·questions=2 (1246ms) |
| 마지막 웹 배포 success | 2026-11-24 |

## 배운 점 / 재발 방지

- 무료 Render DB 만료 일정을 달력에 두고, 만료 전에 재생성·Secret 갱신.
- FE “서버 안 됨” 1차 점검: `/health` 타임아웃 여부 + 최근 ingest 실패 로그.

## 이력서 반영 여부

- [ ] `resume-bullets.md` 에 추가함
