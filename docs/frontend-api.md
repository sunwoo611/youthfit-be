# 프론트엔드 API 연동 가이드

YouthFit 백엔드와 Next.js 프론트를 붙일 때 쓰는 계약서입니다.  
요청·응답 **필드 정의의 단일 진실 소스**는 Swagger(`/docs`, `/openapi.json`)입니다. 이 문서는 화면 흐름·주의사항·예시만 담습니다.

| 항목 | 값 |
|------|-----|
| 로컬 Base URL | `http://localhost:8000` |
| 개발 서버 Base URL | 팀에서 공유한 Render URL (예: `https://youthfit-api-….onrender.com`) |
| Swagger | `{Base URL}/docs` |
| OpenAPI JSON | `{Base URL}/openapi.json` |
| 인증 | 없음 (공개 API) |
| Content-Type | `application/json` |
| 추천 범위 | **청년 정책만** (적금 API 없음) |

CORS 기본 허용: `http://localhost:3000`. 배포 도메인·Vercel 프리뷰는 백엔드 `CORS_ORIGINS` / `CORS_ORIGIN_REGEX`에 추가가 필요합니다.

Render 무료 플랜은 약 15분 무요청 후 슬립하며, 깨어날 때 첫 요청이 최대 약 1분 걸릴 수 있습니다. UX에 “잠시만 기다려 주세요”를 두는 것을 권장합니다.

---

## 권장 화면 흐름 (MVP)

메인 UX는 **세션 API**입니다. `POST /v1/candidates`는 디버그·단독 필터용입니다.

```text
1. 자연어 한 문장 입력
   → POST /v1/sessions
   ← session_id, questions(칩 3~5개), candidates(1차 후보 상위 50)

2. 칩 답변 (질문마다 option.value 전송)
   → POST /v1/sessions/{session_id}/answers
   ← 좁혀진 candidates (같은 SessionResponse 형태)

3. 결과 카드
   → POST /v1/sessions/{session_id}/recommendations
   ← 상위 최대 5건 + reason + check_points

(선택) 새로고침·복원
   → GET /v1/sessions/{session_id}
```

세션은 생성 시각 기준 **24시간** 유효합니다. 만료·없으면 `404` (`세션이 없거나 만료됨`).

---

## 엔드포인트 요약

| Method | Path | 용도 | 성공 | 제한 (10분 창) |
|--------|------|------|------|----------------|
| `GET` | `/health` | 생존·DB 상태 | 200 | 없음 |
| `POST` | `/v1/sessions` | 세션 생성 + 역질문 | **201** | IP 20 / 전체 300 |
| `GET` | `/v1/sessions/{id}` | 세션 조회 | 200 | 없음 |
| `POST` | `/v1/sessions/{id}/answers` | 답 반영·재필터 | 200 | IP 60 / 전체 900 |
| `POST` | `/v1/sessions/{id}/recommendations` | 최종 추천+사유 | 200 | IP 10 / 전체 120 |
| `POST` | `/v1/candidates` | 룰 필터만 (LLM·세션 없음) | 200 | 없음 |

초과 시 `429` + 헤더 `Retry-After`(초). 본문 메시지: `요청이 너무 많음. 잠시 후 다시 시도`.

공통 오류:

| 코드 | 상황 |
|------|------|
| 404 | 세션 없음·만료 |
| 422 | 스키마 불일치, 알 수 없는 질문/선택지, 같은 질문 중복 답 |
| 429 | 요청 제한 |
| 503 | DB 연결 불가 (세션 생성·선정 사유) |

---

## 1) 세션 생성 — `POST /v1/sessions`

자연어 `query`는 **필수**. 나이·시도·취업상태·관심분야는 선택이며, 비어 있으면 `query`에서 규칙 사전으로만 보조 추출합니다.

### 요청 예시

```json
{
  "query": "서울 사는 25살 취준생인데 주거 지원 받을 수 있을까요?",
  "age": 25,
  "sido_code": "11",
  "employment_status": "unemployed",
  "interests": ["housing"]
}
```

필드만으로도 가능하지만 `query`는 최소 1자 이상이어야 합니다. 추출만 맡기려면:

```json
{ "query": "서울 사는 25살 취준생" }
```

### 응답에서 FE가 쓸 필드

| 필드 | 설명 |
|------|------|
| `session_id` | 이후 요청에 사용 (UUID) |
| `expires_at` | 만료 시각 |
| `questions[]` | 칩 UI. `id`, `text`, `type`, `options[{value,label}]`, `affected` |
| `answers` | 생성 직후는 `[]` |
| `candidates` | 1차 후보 (`as_of`, `conditions`, `policies.total`, `policies.items`) |

질문 `variable`은 서버가 고릅니다. 현재 가능 값:

- `marriage` — 혼인
- `school` — 학력
- `income_bucket` — 본인 연소득 구간
- `sigungu` — 시군구 (시도에 이름표가 있을 때만)

`text`는 LLM이 다듬을 수 있고, 실패·키 없으면 템플릿 문구입니다. **`id`·`variable`·`options`는 서버 고정**이므로 FE가 바꾸지 않습니다.

---

## 2) 답변 — `POST /v1/sessions/{session_id}/answers`

칩에서 고른 **`option.value`** 만 보냅니다. 표시 문구는 `label` / `text`를 씁니다.

- 답한 질문만 넣어도 됩니다.
- **다시내면 이전 답 전체를 덮어씁니다** (부분 병합이 아님). 예: `q1`만 다시 내면 `q2` 답도 사라집니다.
- 현재 질문은 사실상 `type: "single"`입니다. `values`는 배열이지만 단일 선택이면 원소 1개.

### 요청 예시

```json
{
  "answers": [
    { "question_id": "q1", "values": ["college_graduated"] },
    { "question_id": "q2", "values": ["single"] }
  ]
}
```

응답은 세션 생성과 같은 `SessionResponse`이며, `candidates`가 좁혀진 상태입니다. 중간 화면에서 후보 수를 보여 주려면 `candidates.policies.total`을 쓰면 됩니다.

---

## 3) 최종 추천 — `POST /v1/sessions/{session_id}/recommendations`

요청 body 없음. 답을 반영한 상위 **최대 5건**과 선정 사유를 돌려줍니다.

첫 호출은 LLM이 있으면 수 초(실측 중앙값 약 3초대, 최대 약 15초 대기 가능), 같은 답으로 다시 치면 캐시를 씁니다. 답을 바꾸면 캐시는 무효화됩니다.

### 응답 구조 (요약)

```json
{
  "session_id": "…",
  "as_of": "2026-11-16",
  "items": [
    {
      "policy": { "plcy_no": "…", "name": "…", "official_url": "https://…", "…": "…" },
      "reason": "…",
      "check_points": ["소득 상한을 공식 공고에서 확인해 주세요"],
      "reason_source": "llm"
    }
  ]
}
```

### FE 필수 규칙 (환각 방지)

1. **카드의 링크·마감일·자격 표시**는 항상 `items[].policy`의 DB 값만 사용합니다.  
   - 링크: `policy.official_url`  
   - 출처: `policy.url_source` (`apply` | `reference` | `ontong_detail`)  
   - 신청 기간: `apply_start` / `apply_end` / `biz_end`  
   - 신청 가능 여부: `availability` (`open` | `upcoming` | `unverified` …; 후보 목록에는 마감(`closed`)이 이미 제외됨)
2. `reason` / `check_points`는 **서술용 문구**입니다. 여기서 URL·날짜를 파싱해 링크로 쓰지 마세요.
3. `reason_source: "template"`도 정상 응답입니다 (키 없음·LLM 실패·검증 탈락). 별도 에러 UI를 띄울 필요 없습니다.
4. `check_points`는 사용자가 직접 확인할 조건(최대 3개). 카드 하단에 체크리스트로 보여 주면 됩니다.

---

## 정책 카드 필드 (`PolicyCandidate`)

결과·중간 후보에 공통입니다.

| 필드 | 설명 |
|------|------|
| `plcy_no` | 정책 ID (키) |
| `name` | 정책명 |
| `org_name` | 주관 기관 |
| `categories` | 카테고리 문자열 목록 |
| `availability` | 신청 가능 상태 |
| `apply_start` / `apply_end` / `biz_end` | 날짜 또는 `null` |
| `eligibility` | 나이·지역·취업 조건 요약 |
| `official_url` | 공식 링크 (DB만) |
| `url_source` | 링크가 신청/참고/온통상세 중 어디인지 |

`eligibility.region_scope`:

- `national` — 전국
- `sido` — 시도 전체
- `sigungu` — 일부 시군구만 (거주지 확인 필요할 수 있음)

---

## 입력 enum 참고

프론트가 폼으로 넘길 때 쓰는 값입니다. 칩 선택지는 세션 응답의 `options`를 그대로 쓰세요.

### `employment_status`

| value | 의미 |
|-------|------|
| `employed` | 재직 |
| `self_employed` | 자영업 |
| `unemployed` | 미취업 |
| `freelancer` | 프리랜서 |
| `day_laborer` | 일용근로 |
| `founder` | (예비)창업 |
| `short_term` | 단기근로 |
| `farmer` | 영농 |

### `interests`

`housing` | `jobs` | `finance` | `education` | `welfare` | `participation`

### `sido_code` (법정 시도 2자리)

| 코드 | 시도 |
|------|------|
| `11` | 서울 |
| `26` | 부산 |
| `27` | 대구 |
| `28` | 인천 |
| `30` | 대전 |
| `31` | 울산 |
| `36` | 세종 |
| `41` | 경기 |
| `43` | 충북 |
| `44` | 충남 |
| `47` | 경북 |
| `48` | 경남 |
| `50` | 제주 |
| `51` | 강원 |
| `52` | 전북 |
| `12` | 전남광주통합 |

시도를 안 주면 **전국 정책만** 고릅니다. 알 수 없는 코드는 `422`입니다.

### 역질문 답 `value` (서버가 options로 내려줌)

혼인: `married` | `single`  
학력: `below_high_school` | `high_school_enrolled` | `high_school_expected` | `high_school_graduated` | `college_enrolled` | `college_expected` | `college_graduated` | `graduate_school`  
소득: `up_to_3000` | `up_to_5000` | `up_to_7000` | `over_7000`  
시군구: 법정 코드(또는 경기처럼 시 단위로 묶인 value). **응답 options의 value를 그대로 사용**.

---

## 보조: `POST /v1/candidates`

세션 없이 룰 필터만 필요할 때 사용합니다. LLM·세션·역질문을 쓰지 않습니다. MVP 메인 플로우는 세션을 쓰세요.

요청 필드는 세션 생성과 유사하나 `query`가 선택입니다.

```json
{
  "age": 25,
  "sido_code": "11",
  "employment_status": "unemployed",
  "interests": ["housing"]
}
```

---

## `GET /health`

배포·기동 확인용.

```json
{ "status": "ok", "env": "dev", "database": "up" }
```

- `status`: `ok` | `degraded`
- `database`: `up` | `down` | `not_configured`

---

## 복사해서 쓰는 호출 예시 (PowerShell)

Base URL을 환경에 맞게 바꿉니다.

```powershell
$base = "http://localhost:8000"

# 1) 세션 생성
$created = Invoke-RestMethod -Method Post -Uri "$base/v1/sessions" `
  -ContentType "application/json" `
  -Body '{"query":"서울 사는 25살 취준생"}'
$sid = $created.session_id
$created.questions | Format-Table id, variable, text

# 2) 답변 (questions의 options.value를 확인한 뒤 채움)
$answers = @{
  answers = @(
    @{ question_id = "q1"; values = @("college_graduated") }
  )
} | ConvertTo-Json -Depth 5
$answered = Invoke-RestMethod -Method Post -Uri "$base/v1/sessions/$sid/answers" `
  -ContentType "application/json" -Body $answers

# 3) 추천
$rec = Invoke-RestMethod -Method Post -Uri "$base/v1/sessions/$sid/recommendations"
$rec.items | ForEach-Object {
  [pscustomobject]@{
    name = $_.policy.name
    url = $_.policy.official_url
    reason_source = $_.reason_source
    reason = $_.reason
  }
}
```

---

## 프론트 구현 체크리스트

- [ ] 입력 → 세션 생성 → 칩 → 답변 → 추천 순서로만 메인 플로우 구성
- [ ] 칩은 `options[].value` 전송, UI 문구는 `label`/`text`
- [ ] 답 재제출 시 “전체 덮어쓰기” 동작에 맞게 로컬 상태 관리
- [ ] 결과 카드 링크는 `policy.official_url`만 사용
- [ ] `reason_source === "template"`을 에러로 취급하지 않음
- [ ] `404` 시 세션 만료 UI → 처음부터 다시
- [ ] `429` 시 `Retry-After` 후 재시도 안내
- [ ] 개발 서버 슬립(첫 요청 지연) UX 고려
- [ ] 프론트 배포 origin을 백엔드 CORS에 등록 요청
- [ ] (알파) 입력 문장·답이 분석용으로 최대 약 30일 보관될 수 있음 — 안내 문구는 팀 협의

---

## 스키마가 바뀌면

1. `{Base URL}/docs`에서 최신 요청·응답을 확인합니다.
2. 이 문서의 **흐름·주의사항**만 갱신합니다. 필드 전부 복붙하지 않습니다.
3. 문의는 백엔드 담당에게 Base URL + 재현용 JSON을 함께 보내 주세요.
