# 002. 스키마: 정규화 컬럼 + 원문 raw JSONB 동시 저장

- 상태: Superseded by 010 (적금 테이블 3개와 그 키·FK 규칙만. 정규화 컬럼 + raw JSONB 방식은 유효)
- 날짜: 2026-10-17

## Context (배경)

- 온통청년 `getPlcy`는 정책 1건에 필드가 60개 이상이고, 코드값(`mrgSttsCd`, `jobCd` 등)과 자유 텍스트가 섞여 있다. 금감원 적금은 `baseList` + `optionList`(기간별 금리) 2단 구조다.
- 1차 룰 필터에는 나이·지역·취업상태·마감일 등 일부 필드만 필요하다. 하지만 3차(LLM 선정 사유, 근거 매칭)에서 어떤 필드가 더 필요할지는 아직 모른다.
- 정규화 규칙(공백 날짜, `aplyYmd` 기간 문자열, `zipCd` 시군구 목록)은 데이터를 보면서 계속 바뀔 수 있다.

## Decision (결정)

필터·정렬에 쓰는 필드는 **타입이 있는 컬럼**으로 두고, API 원문 레코드는 **`raw JSONB`** 에 함께 저장한다. 마이그레이션은 Alembic(async 템플릿)으로 관리한다.

- 테이블 5개: `policies`, `financial_companies`, `saving_products`, `saving_product_options`, `ingest_runs`
- 다중 코드(`job_codes`, `sido_codes`, `sigungu_codes` 등)는 `text[]` 배열로 두고, 필터에 쓰는 `sido_codes`·`job_codes`에 GIN 인덱스를 건다.
- 정책 대·중분류(`lclsfNm`, `mclsfNm`)도 콤마로 여러 값이 올 수 있어 `categories`·`subcategories` 배열로 둔다. 관심분야 필터는 `categories && ARRAY[...]`(겹침)로 걸고 `categories`에 GIN 인덱스를 건다. 단일 문자열 + 길이 제한 컬럼은 길이 초과 시 적재 실패, 다중 분류 정책 누락 위험이 있어 채택하지 않았다.
- `saving_products.fin_co_no`는 `financial_companies`에 **FK를 걸지 않는다**. `companySearch`에 회사가 빠져도 상품 적재가 실패하지 않게 하고, 매핑 성공률은 품질 리포트에서 측정한다.
- 금리 옵션은 `(fin_co_no, fin_prdt_cd, rate_type, reserve_type, term_months)` 유니크로 upsert 키를 둔다.

## Alternatives (검토한 대안)

| 대안 | 장점 | 단점 | 채택하지 않은 이유 |
|------|------|------|--------------------|
| 전부 정규화 컬럼 | 스키마가 명확 | 필드 60개+ 매핑, 미사용 필드 관리 비용 | 아직 필요 필드가 확정되지 않음 |
| `raw JSONB`만 저장 | 적재 단순 | 필터 SQL이 JSON 경로 연산투성이, 타입·인덱스 약함 | 1차 필터 성능·가독성 저하 |
| 코드 배열을 별도 조인 테이블로 | 정규형 | 조인 증가, upsert 복잡 | 건수가 수천 건 수준이라 배열 + GIN으로 충분 |

## Consequences (결과)

- 정규화 규칙이 바뀌어도 외부 API를 다시 호출하지 않고 `raw`에서 재정규화할 수 있다.
- 저장 용량이 원문만큼 늘어난다 (정책 약 3,264건 기준 부담 적음, 측정 전).
- 컬럼과 `raw`가 어긋나지 않도록 적재 코드가 항상 둘을 같이 써야 한다.
- 후속 과제: 정규화 함수(`app/ingest/normalize.py`) 단위 테스트, 품질 리포트에서 필드 채움률 측정.

## 이력서·면접용 한 줄

"필드 요구가 유동적인 공공 API 데이터를 정규화 컬럼 + 원문 JSONB로 이중 저장해, 외부 재호출 없이 재정규화 가능한 스키마를 설계함"
