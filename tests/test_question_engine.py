"""역질문 엔진. 점수·선정은 순수 함수로, 파이썬 판정 == SQL 필터는 롤백 트랜잭션에서 검증한다."""

import itertools

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from app.ingest import codes, sigungu
from app.schemas.candidates import AppliedConditions, IncomeBucket, Marriage, School
from app.schemas.sessions import QuestionVariable
from app.services import question_engine
from app.services.candidate_service import (
    REGION_NATIONAL,
    REGION_SIDO,
    REGION_SIGUNGU,
    SCHOOL_CODE,
    policy_query,
)
from app.services.question_engine import (
    REGISTRY,
    apply_answer,
    removal_score,
    select_questions,
)
from tests.test_candidates import TODAY, _insert_policies, _policy

MARRIED, SINGLE = "0055001", "0055002"
COLLEGE_ENROLLED, HIGH_SCHOOL_GRADUATED = "0049005", "0049004"


def _conditions(sido_code: str | None = "11", **overrides: object) -> AppliedConditions:
    return AppliedConditions(
        age=None,
        sido_code=sido_code,
        employment_status=None,
        interests=[],
        from_query=[],
        **overrides,
    )


def _row(**overrides: object) -> dict:
    row: dict = {
        "marriage_code": None,
        "school_codes": [],
        "earn_cond_code": None,
        "earn_max": None,
        "region_rank": REGION_SIDO,
        "sigungu_codes": [],
    }
    row.update(overrides)
    return row


def _variables(rows: list[dict], conditions: AppliedConditions) -> list[QuestionVariable]:
    return [q.variable for q in select_questions(rows, conditions)]


# --- 점수 ---


def test_removal_score_averages_removed_ratio_over_options():
    rows = [
        *(_row(marriage_code=MARRIED) for _ in range(3)),
        _row(marriage_code=SINGLE),
        _row(marriage_code=codes.MARRIAGE_ANY),
        *(_row() for _ in range(5)),
    ]
    variable = REGISTRY[QuestionVariable.MARRIAGE]
    score, affected = removal_score(rows, variable, variable.options(_conditions()))
    # 기혼이면 미혼 전용 1건, 미혼이면 기혼 전용 3건이 빠진다.
    assert score == pytest.approx((1 + 3) / (2 * 10))
    assert affected == 4


def test_removal_score_of_empty_candidates_is_zero():
    variable = REGISTRY[QuestionVariable.SCHOOL]
    assert removal_score([], variable, variable.options(_conditions())) == (0.0, 0)


def test_school_etc_and_full_listing_are_not_restrictions():
    rows = [
        _row(school_codes=[codes.SCHOOL_ETC]),
        _row(school_codes=[codes.SCHOOL_ANY]),
        _row(school_codes=list(SCHOOL_CODE.values())),
    ]
    variable = REGISTRY[QuestionVariable.SCHOOL]
    assert removal_score(rows, variable, variable.options(_conditions())) == (0.0, 0)


@pytest.mark.parametrize(
    ("school", "kept"),
    [
        (School.COLLEGE_EXPECTED, {COLLEGE_ENROLLED, "0049006"}),
        (School.GRADUATE_SCHOOL, {"0049007", "0049008"}),
        (School.COLLEGE_ENROLLED, {COLLEGE_ENROLLED}),
        (School.COLLEGE_GRADUATED, {"0049007"}),
    ],
)
def test_school_answer_keeps_contained_statuses_only(school: School, kept: set[str]):
    variable = REGISTRY[QuestionVariable.SCHOOL]
    singles = [HIGH_SCHOOL_GRADUATED, COLLEGE_ENROLLED, "0049006", "0049007", "0049008"]
    assert {
        code for code in singles if variable.keeps(_row(school_codes=[code]), school.value)
    } == kept


def test_income_uses_only_trusted_range_max():
    rows = [
        _row(earn_cond_code=codes.EARN_RANGE, earn_max=5000),
        _row(earn_cond_code=codes.EARN_RANGE, earn_max=534),
        _row(earn_cond_code=codes.EARN_RANGE, earn_max=43_056_240),
        _row(earn_cond_code="0043003", earn_max=2000),
    ]
    variable = REGISTRY[QuestionVariable.INCOME_BUCKET]
    score, affected = removal_score(rows, variable, variable.options(_conditions()))
    # 상한 5,000 정책은 5,000 초과 두 구간에서만 빠진다.
    assert affected == 1
    assert score == pytest.approx(2 / (4 * 4))


# --- 선정 ---


def _mixed_rows() -> list[dict]:
    """학력 > 시군구 > 혼인 > 소득 순으로 점수가 나오는 후보 100건."""
    return [
        *(_row(school_codes=[COLLEGE_ENROLLED]) for _ in range(30)),
        *(_row(region_rank=REGION_SIGUNGU, sigungu_codes=["11110"]) for _ in range(10)),
        *(_row(marriage_code=MARRIED) for _ in range(8)),
        *(_row(earn_cond_code=codes.EARN_RANGE, earn_max=5000) for _ in range(5)),
        *(_row() for _ in range(47)),
    ]


def test_selects_unknown_variables_by_score():
    questions = select_questions(_mixed_rows(), _conditions())
    assert [(q.id, q.variable, q.affected) for q in questions] == [
        ("q1", QuestionVariable.SCHOOL, 30),
        ("q2", QuestionVariable.SIGUNGU, 10),
        ("q3", QuestionVariable.MARRIAGE, 8),
        ("q4", QuestionVariable.INCOME_BUCKET, 5),
    ]
    assert all(q.type == "single" for q in questions)
    assert questions[1].text == "서울특별시 어느 시군구에 사시나요?"
    assert len(questions[1].options) == 25
    assert questions[0].options[4].model_dump() == {
        "value": "college_enrolled",
        "label": "대학 재학",
    }


def test_known_variables_are_not_asked():
    conditions = _conditions(
        school=School.COLLEGE_ENROLLED,
        marriage=Marriage.SINGLE,
        sigungu_codes=["11110"],
    )
    assert _variables(_mixed_rows(), conditions) == [QuestionVariable.INCOME_BUCKET]


def test_variables_below_threshold_are_not_asked():
    few = [_row(marriage_code=MARRIED), _row(marriage_code=MARRIED), *(_row() for _ in range(8))]
    assert _variables(few, _conditions()) == []

    # 3건이 빠지지만 기대 제거율 3 / (2 * 400) < 0.01
    diluted = [*(_row(marriage_code=MARRIED) for _ in range(3)), *(_row() for _ in range(397))]
    assert _variables(diluted, _conditions()) == []
    assert _variables([], _conditions()) == []


def test_tied_scores_keep_registry_order():
    # 기혼 전용 6건(2개 중 1개 선택지에서 빠짐)과 대학 재학 전용 4건(대학 재학·대졸 예정을 뺀
    # 8개 중 6개)이 둘 다 0.15.
    rows = [
        *(_row(marriage_code=MARRIED) for _ in range(6)),
        *(_row(school_codes=[COLLEGE_ENROLLED]) for _ in range(4)),
        *(_row() for _ in range(10)),
    ]
    marriage, school = REGISTRY[QuestionVariable.MARRIAGE], REGISTRY[QuestionVariable.SCHOOL]
    base = _conditions()
    assert removal_score(rows, marriage, marriage.options(base))[0] == pytest.approx(
        removal_score(rows, school, school.options(base))[0]
    )
    assert _variables(rows, base) == [QuestionVariable.MARRIAGE, QuestionVariable.SCHOOL]


def test_question_count_is_capped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(question_engine, "MAX_QUESTIONS", 2)
    assert _variables(_mixed_rows(), _conditions()) == [
        QuestionVariable.SCHOOL,
        QuestionVariable.SIGUNGU,
    ]


@pytest.mark.parametrize("sido_code", [None, "12", "28", "36"])
def test_sigungu_is_skipped_without_name_table(sido_code: str | None):
    rows = [_row(region_rank=REGION_SIGUNGU, sigungu_codes=["12110"]) for _ in range(10)]
    assert QuestionVariable.SIGUNGU not in _variables(rows, _conditions(sido_code))


def test_sigungu_option_matches_districts_and_sido_wide_code():
    keeps = REGISTRY[QuestionVariable.SIGUNGU].keeps
    suwon_gu = _row(region_rank=REGION_SIGUNGU, sigungu_codes=["41113"])
    hwaseong_gu = _row(region_rank=REGION_SIGUNGU, sigungu_codes=["41595"])
    daejeon_all = _row(region_rank=REGION_SIGUNGU, sigungu_codes=["30000"])
    assert keeps(suwon_gu, "41110")
    assert not keeps(suwon_gu, "41130")
    assert keeps(hwaseong_gu, "41590")
    assert all(keeps(daejeon_all, code) for code in sigungu.SIGUNGU["30"])
    assert keeps(_row(region_rank=REGION_SIDO, sigungu_codes=["41113"]), "41130")


def test_sigungu_table_codes_are_consistent():
    assert set(sigungu.SIGUNGU) <= set(codes.SIDO) - {"12", "28", "36"}
    for sido, names in sigungu.SIGUNGU.items():
        assert all(len(code) == 5 and code.startswith(sido) for code in names)
    for city, districts in sigungu.DISTRICTS.items():
        assert city in sigungu.SIGUNGU[city[:2]]
        assert all(code[:4] == city[:4] for code in districts)


# --- 답 반영 ---


def test_apply_answer_sets_condition_fields():
    base = _conditions("41")
    assert apply_answer(base, QuestionVariable.MARRIAGE, "single").marriage is Marriage.SINGLE
    assert apply_answer(base, QuestionVariable.SCHOOL, "graduate_school").school is School(
        "graduate_school"
    )
    income = apply_answer(base, QuestionVariable.INCOME_BUCKET, "up_to_5000")
    assert income.income_bucket is IncomeBucket.UP_TO_5000
    assert apply_answer(base, QuestionVariable.SIGUNGU, "41110").sigungu_codes == [
        "41000",
        "41110",
        "41111",
        "41113",
        "41115",
        "41117",
    ]
    assert base.sigungu_codes == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (QuestionVariable.MARRIAGE, "divorced"),
        (QuestionVariable.SCHOOL, "0049005"),
        (QuestionVariable.INCOME_BUCKET, "up_to_1000"),
        (QuestionVariable.SIGUNGU, "41111"),
        (QuestionVariable.SIGUNGU, "11110"),
    ],
)
def test_apply_answer_rejects_values_outside_options(variable: QuestionVariable, value: str):
    with pytest.raises(ValueError):
        apply_answer(_conditions("41"), variable, value)


# --- DB 통합: 파이썬 판정 == SQL 필터 ---

MARRIAGES = [None, codes.MARRIAGE_ANY, MARRIED, SINGLE]
SCHOOLS = [
    [],
    [codes.SCHOOL_ANY],
    [codes.SCHOOL_ETC],
    [COLLEGE_ENROLLED],
    [HIGH_SCHOOL_GRADUATED, "0049007"],
    [COLLEGE_ENROLLED, codes.SCHOOL_ETC],
    ["0049002"],
    ["0049007"],
    ["0049003", HIGH_SCHOOL_GRADUATED, "0049006", "0049007"],
]
EARNS = [
    (None, None),
    (codes.EARN_ANY, None),
    ("0043003", 2000),
    (codes.EARN_RANGE, None),
    (codes.EARN_RANGE, 999),
    (codes.EARN_RANGE, 1000),
    (codes.EARN_RANGE, 3000),
    (codes.EARN_RANGE, 5000),
    (codes.EARN_RANGE, 7000),
    (codes.EARN_RANGE, 20000),
    (codes.EARN_RANGE, 20001),
]
# 시도별 (sido_codes, sigungu_codes). 전국 정책은 여러 시도에 걸쳐야 시도 시군구 수 계산에서 빠진다.
NATIONAL_REGION = (list(codes.SIDO), [f"{i:05d}" for i in range(codes.REGION_FREE_MIN_SIGUNGU)])
REGIONS: dict[str, list[tuple[list[str], list[str]]]] = {
    "11": [
        (["11"], list(sigungu.SIGUNGU["11"])),
        (["11"], ["11110"]),
        (["11"], ["11140", "11200"]),
        (["11"], ["11000"]),
        NATIONAL_REGION,
    ],
    # 구가 있는 시는 정책에 시 코드(41110)와 구 코드(41113)가 따로 온다.
    "41": [
        (["41"], list(sigungu.SIGUNGU["41"])),
        (["41"], ["41113"]),
        (["41"], ["41110"]),
        (["41"], ["41595", "41130"]),
        (["41"], ["41000"]),
        NATIONAL_REGION,
    ],
}


async def _equivalence_candidates(db_conn: AsyncConnection, sido_code: str) -> list[dict]:
    """혼인·학력·소득·지역 값의 모든 조합을 넣고, 답이 없을 때의 후보 전부를 돌려준다."""
    rows = [
        _policy(
            f"QE-{i:04d}",
            marriage_code=marriage,
            school_codes=school,
            earn_cond_code=earn_code,
            earn_max=earn_max,
            sido_codes=sido_codes,
            sigungu_codes=sigungu_codes,
        )
        for i, (marriage, school, (earn_code, earn_max), (sido_codes, sigungu_codes)) in enumerate(
            itertools.product(MARRIAGES, SCHOOLS, EARNS, REGIONS[sido_code])
        )
    ]
    await _insert_policies(db_conn, *rows)
    query = policy_query(_conditions(sido_code), TODAY, len(rows))
    candidates = [dict(row) for row in (await db_conn.execute(query)).mappings()]
    assert len(candidates) == len(rows)
    assert {row["region_rank"] for row in candidates} == {
        REGION_SIDO,
        REGION_NATIONAL,
        REGION_SIGUNGU,
    }
    return candidates


async def _sql_plcy_nos(db_conn: AsyncConnection, conditions: AppliedConditions) -> set[str]:
    result = await db_conn.execute(policy_query(conditions, TODAY, limit=10_000))
    return {row["plcy_no"] for row in result.mappings()}


@pytest.mark.parametrize("sido_code", ["11", "41"])
async def test_python_keeps_matches_sql_filters(db_conn: AsyncConnection, sido_code: str):
    candidates = await _equivalence_candidates(db_conn, sido_code)
    base = _conditions(sido_code)
    for variable in REGISTRY.values():
        for option in variable.options(base):
            sql = await _sql_plcy_nos(db_conn, apply_answer(base, variable.name, option.value))
            python = {row["plcy_no"] for row in candidates if variable.keeps(row, option.value)}
            assert sql == python, (variable.name, option.value)


async def test_combined_answers_match_all_python_keeps(db_conn: AsyncConnection):
    candidates = await _equivalence_candidates(db_conn, "41")
    base = _conditions("41")
    options = {name: variable.options(base) for name, variable in REGISTRY.items()}
    fixed = (QuestionVariable.MARRIAGE, QuestionVariable.SCHOOL, QuestionVariable.INCOME_BUCKET)
    sigungu_options = options[QuestionVariable.SIGUNGU]
    combos = itertools.product(*(options[name] for name in fixed))
    for i, combo in enumerate(combos):
        answers = [
            *zip(fixed, (option.value for option in combo), strict=True),
            (QuestionVariable.SIGUNGU, sigungu_options[i % len(sigungu_options)].value),
        ]
        conditions = base
        for name, value in answers:
            conditions = apply_answer(conditions, name, value)
        sql = await _sql_plcy_nos(db_conn, conditions)
        python = {
            row["plcy_no"]
            for row in candidates
            if all(REGISTRY[name].keeps(row, value) for name, value in answers)
        }
        assert sql == python, answers
