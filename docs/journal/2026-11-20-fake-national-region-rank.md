# 2026-11-20 가짜 전국 region_rank sido 오분류

## 상황 / 증상

가짜 전국을 해당 시도에서 `region_scope=sigungu`로 내려야 하는데, zip이 그 시도 시군구를 대부분 포함하면 `region_rank`가 시도 전체(`sido`)로 올라갔다. 테스트 `NATIONAL`이 `00000..00199`라 법정 앞2자리 경로를 안 타 회귀를 못 잡았다.

## 원인 분석

`region_rank_expr`는 유효 전국이 아니면 바로 `in_sido >= sido_total * 0.8`을 본다. 실제 전국 zip은 경북 시군구를 거의 다 넣어 비율을 통과한다.

## 해결

`fake_national_expr()` → `REGION_SIGUNGU` 분기를 시도 전체 분기 앞에 넣었다. 픽스처는 경북 시군구 전부 + 타 시도 혼합 zip(200개+)과 경북 단일 시도 행으로 바꿨다.

## 결과 (수치)

- 혼합 zip에서 경북 코드 수 = `len(sigungu.SIGUNGU["47"])` (23), 임계값 `23 * 0.8` → 수정 전이면 `sido`, 수정 후 `sigungu` (테스트 고정).
- `tests/test_candidates.py -k fake_national` 관련 10 passed.

## 이력서 반영 여부

- [ ] `resume-bullets.md` 에 추가함
