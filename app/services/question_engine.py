"""역질문 엔진. 후보를 가장 많이 가르는 자격 변수를 골라 템플릿 질문을 만든다 (DB·LLM 미사용).

후보 행은 `policy_query` 결과 행이고, 변수별 판정은 `_policy_filters` 의 SQL 조건과 같아야 한다.
점수(기대 제거율)는 선택지가 고르게 답해진다고 보고, 답한 뒤 빠질 후보 비율의 평균이다.
변수 선정 근거는 docs/journal/2026-11-02-question-engine.md.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.ingest import codes, sigungu
from app.schemas.candidates import AppliedConditions, IncomeBucket, Marriage, School
from app.schemas.sessions import Question, QuestionOption, QuestionVariable
from app.services.candidate_service import (
    EARN_MAX_TRUSTED_MAX,
    EARN_MAX_TRUSTED_MIN,
    INCOME_BUCKET_FLOOR,
    MARRIAGE_CODE,
    REGION_NATIONAL,
    SCHOOL_CODE,
    SCHOOL_MATCH,
)

Row = Mapping[str, Any]

MAX_QUESTIONS = 5
# 빠질 수 있는 후보가 이보다 적거나 점수가 이보다 낮은 변수는 묻지 않는다.
MIN_AFFECTED = 3
MIN_SCORE = 0.01

INCOME_LABEL: dict[IncomeBucket, str] = {
    IncomeBucket.UP_TO_3000: "3,000만원 이하",
    IncomeBucket.UP_TO_5000: "3,000만원 초과 ~ 5,000만원 이하",
    IncomeBucket.UP_TO_7000: "5,000만원 초과 ~ 7,000만원 이하",
    IncomeBucket.OVER_7000: "7,000만원 초과",
}


@dataclass(frozen=True)
class Variable:
    name: QuestionVariable
    text: str
    known: Callable[[AppliedConditions], bool]
    options: Callable[[AppliedConditions], list[QuestionOption]]
    keeps: Callable[[Row, str], bool]
    answer: Callable[[str], dict[str, Any]]


def _keeps_marriage(row: Row, value: str) -> bool:
    code = row["marriage_code"]
    return code is None or code in (codes.MARRIAGE_ANY, MARRIAGE_CODE[Marriage(value)])


def _keeps_school(row: Row, value: str) -> bool:
    allowed = {codes.SCHOOL_ANY, codes.SCHOOL_ETC, *SCHOOL_MATCH[School(value)]}
    return not row["school_codes"] or not allowed.isdisjoint(row["school_codes"])


def _keeps_income(row: Row, value: str) -> bool:
    floor = INCOME_BUCKET_FLOOR[IncomeBucket(value)]
    earn_max = row["earn_max"]
    return (
        floor == 0
        or row["earn_cond_code"] != codes.EARN_RANGE
        or earn_max is None
        or not EARN_MAX_TRUSTED_MIN <= earn_max <= EARN_MAX_TRUSTED_MAX
        or earn_max > floor
    )


def _keeps_sigungu(row: Row, value: str) -> bool:
    if row["region_rank"] == REGION_NATIONAL:
        return True
    return not set(sigungu.match_codes(value)).isdisjoint(row["sigungu_codes"])


def _sigungu_options(conditions: AppliedConditions) -> list[QuestionOption]:
    names = sigungu.SIGUNGU.get(conditions.sido_code or "", {})
    return [QuestionOption(value=code, label=name) for code, name in names.items()]


VARIABLES: tuple[Variable, ...] = (
    Variable(
        name=QuestionVariable.MARRIAGE,
        text="결혼하셨나요?",
        known=lambda c: c.marriage is not None,
        options=lambda _: [
            QuestionOption(value=m.value, label=codes.MARRIAGE[MARRIAGE_CODE[m]]) for m in Marriage
        ],
        keeps=_keeps_marriage,
        answer=lambda value: {"marriage": value},
    ),
    Variable(
        name=QuestionVariable.SCHOOL,
        text="학력은 어떻게 되나요? (재학·졸업 예정 포함)",
        known=lambda c: c.school is not None,
        options=lambda _: [
            QuestionOption(value=s.value, label=codes.SCHOOL[SCHOOL_CODE[s]]) for s in School
        ],
        keeps=_keeps_school,
        answer=lambda value: {"school": value},
    ),
    Variable(
        name=QuestionVariable.INCOME_BUCKET,
        text="본인 연소득(세전)은 어느 정도인가요?",
        known=lambda c: c.income_bucket is not None,
        options=lambda _: [
            QuestionOption(value=b.value, label=label) for b, label in INCOME_LABEL.items()
        ],
        keeps=_keeps_income,
        answer=lambda value: {"income_bucket": value},
    ),
    Variable(
        name=QuestionVariable.SIGUNGU,
        text="{sido} 어느 시군구에 사시나요?",
        known=lambda c: bool(c.sigungu_codes),
        options=_sigungu_options,
        keeps=_keeps_sigungu,
        answer=lambda value: {"sigungu_codes": sigungu.match_codes(value)},
    ),
)
REGISTRY: dict[QuestionVariable, Variable] = {v.name: v for v in VARIABLES}


def removal_score(
    rows: Sequence[Row], variable: Variable, options: Sequence[QuestionOption]
) -> tuple[float, int]:
    """(기대 제거율, 어떤 선택지에서든 빠지는 후보 수)."""
    if not rows or not options:
        return 0.0, 0
    removed = affected = 0
    for row in rows:
        dropped = sum(not variable.keeps(row, option.value) for option in options)
        removed += dropped
        affected += dropped > 0
    return removed / (len(rows) * len(options)), affected


def select_questions(rows: Sequence[Row], conditions: AppliedConditions) -> list[Question]:
    """아직 모르는 변수를 점수 순으로 최대 MAX_QUESTIONS 개. 기준 미달 변수는 묻지 않는다."""
    scored: list[tuple[float, int, Variable, list[QuestionOption]]] = []
    for variable in VARIABLES:
        if variable.known(conditions):
            continue
        options = variable.options(conditions)
        if len(options) < 2:
            continue
        score, affected = removal_score(rows, variable, options)
        if affected >= MIN_AFFECTED and score >= MIN_SCORE:
            scored.append((score, affected, variable, options))
    scored.sort(key=lambda item: item[0], reverse=True)
    sido_name = codes.SIDO.get(conditions.sido_code or "", "")
    return [
        Question(
            id=f"q{i}",
            variable=variable.name,
            text=variable.text.format(sido=sido_name),
            type="single",
            options=options,
            affected=affected,
        )
        for i, (_, affected, variable, options) in enumerate(scored[:MAX_QUESTIONS], start=1)
    ]


def apply_answer(
    conditions: AppliedConditions, variable: QuestionVariable, value: str
) -> AppliedConditions:
    """답 하나를 조건에 반영한다. 선택지에 없는 값이면 ValueError(ValidationError 포함)."""
    update = REGISTRY[variable].answer(value)
    return AppliedConditions.model_validate(conditions.model_dump() | update)
