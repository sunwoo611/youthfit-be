# 007. 프론트 협업용 개발 서버: Render + GitHub Actions

- 상태: Accepted
- 날짜: 2026-10-26

## Context (배경)

- 프론트(Next.js)는 별도 레포에서 개발 중이다. 지금은 백엔드를 각자 로컬에서 띄워야 해서 프론트가 실제 API에 붙여 보기 어렵다.
- `main`에 merge되면 공유 서버에 바로 반영되어야 한다. 테스트가 깨진 커밋이 올라가면 안 된다.
- 계획상 클라우드 배포는 9주차(11/17~)이지만, 통합 속도를 위해 개발용 서버를 앞당긴다. 비용은 0원이 목표다.
- 서버에는 CORS 설정이 없어 다른 출처의 브라우저 요청이 막힌다. 플랫폼 DB URL은 `postgresql://` 형식이라 asyncpg 엔진에 그대로 쓸 수 없다.

## Decision (결정)

- **배포**: Render 무료 웹 서비스(Docker) + Render 무료 PostgreSQL 16, 리전 싱가포르. `render.yaml` Blueprint로 정의한다.
- **자동 배포**: `main` push → GitHub Actions CI(ruff, Postgres 서비스 컨테이너에 마이그레이션 후 pytest) → 통과한 커밋만 배포(`autoDeployTrigger: checksPass`).
- **마이그레이션**: 무료 플랜은 pre-deploy 명령이 없어 컨테이너 기동 시 `alembic upgrade head` 후 uvicorn을 띄운다.
- **일 배치**: Render 크론은 유료라 GitHub Actions `schedule`(매일 04:00 KST)로 Render DB 외부 URL에 적재한다. 수동 실행(`workflow_dispatch`)으로 최초 적재도 한다.
- **설정**: `CORS_ORIGINS`(쉼표 구분) + `CORS_ORIGIN_REGEX`(Vercel 프리뷰). `DATABASE_URL`은 `Settings`에서 `postgresql+asyncpg://`, `sslmode`→`ssl`로 바꾼다.

## Alternatives (검토한 대안)

| 대안 | 장점 | 단점 | 채택하지 않은 이유 |
|------|------|------|--------------------|
| Railway | 슬립 없음, 설정 간단 | 체험 크레딧 이후 월 약 5달러 | 개발 단계는 무료로 충분 |
| VPS(EC2·Lightsail) + compose + SSH 배포 | 슬립 없음, 자유도 높음 | 서버·보안 패치·배포 스크립트 직접 관리 | 1인 백엔드에 운영 부담 큼 |
| 로컬 PC + ngrok·Cloudflare Tunnel | 즉시 가능 | PC가 켜져 있어야 함, main 자동 반영 없음 | 요구사항 불충족 |
| Render 크론 잡으로 일 배치 | 같은 네트워크(내부 URL) | 유료 | GitHub Actions로 무료 대체 가능 |

## Consequences (결과)

- 얻는 것: 프론트가 고정 URL과 `/docs`·`/openapi.json`으로 최신 API를 쓴다. 깨진 커밋은 배포되지 않는다.
- 감수하는 것:
  - 무료 웹 서비스(0.1 CPU / 512 MB)는 15분 요청이 없으면 슬립한다. Render 문서 기준 깨어나는 데 약 1분(실측 전). 깰 때마다 `alembic upgrade head`(변경 없으면 no-op)도 다시 돈다.
  - 무료 PostgreSQL(1 GB, 백업 없음, 워크스페이스당 1개)은 생성 30일 뒤 만료, 14일 유예 후 삭제된다. 데이터는 공공 API 미러라 재생성 후 적재 워크플로를 다시 돌리면 되지만, 외부 URL이 바뀌어 GitHub Secret도 갱신해야 한다.
  - Blueprint에 값을 적은 환경 변수(`CORS_ORIGINS`)는 대시보드에서 바꿔도 다음 동기화 때 되돌아간다. 비밀이 아닌 값은 `render.yaml`에서만 바꾼다.
  - GitHub Actions 러너는 해외 IP다. 공공 API가 해외 IP를 막으면 일 배치를 로컬 작업 스케줄러에서 Render 외부 URL로 돌린다(측정 전).
- 후속: 알파 테스트(10주차) 전에 슬립·DB 만료가 문제되면 유료 플랜 또는 Railway로 옮긴다(Dockerfile 기반이라 이전 비용 작음).

## 이력서·면접용 한 줄

"별도 레포 프론트와의 통합을 앞당기려 CI 통과 커밋만 자동 배포되는 무료 개발 서버(Render + GitHub Actions)를 구성하고, 유료 크론 대신 Actions 스케줄로 일 배치를 옮김"
