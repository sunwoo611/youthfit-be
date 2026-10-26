import pytest

from app.schemas.candidates import EmploymentStatus, Interest
from app.services.query_hints import QueryHints, extract_hints


@pytest.mark.parametrize("query", [None, "", "   "])
def test_empty_query_has_no_hints(query: str | None):
    assert extract_hints(query) == QueryHints()


def test_extracts_region_employment_age_and_interests():
    hints = extract_hints("서울 사는 25살 취준생인데 월세 지원이랑 적금 알아보고 싶어요")
    assert hints.sido_code == "11"
    assert hints.employment_status is EmploymentStatus.UNEMPLOYED
    assert hints.age == 25
    assert set(hints.interests) == {Interest.HOUSING, Interest.JOBS, Interest.FINANCE}


@pytest.mark.parametrize(
    ("query", "sido"),
    [
        ("경기 광주시에 살아요", "41"),
        ("광주에서 일하는 직장인", "12"),
        ("전라남도 거주", "12"),
        ("부산", "26"),
        ("어디 사는지 안 말함", None),
    ],
)
def test_region_uses_first_mention(query: str, sido: str | None):
    assert extract_hints(query).sido_code == sido


@pytest.mark.parametrize(
    ("query", "age"),
    [("만 29세 재직자", 29), ("서른 넘은 31세", 31), ("2026년에 지원", None), ("100세", None)],
)
def test_age(query: str, age: int | None):
    assert extract_hints(query).age == age


def test_employment_first_mention_wins():
    hints = extract_hints("직장 다니다 그만두고 창업 준비 중")
    assert hints.employment_status is EmploymentStatus.EMPLOYED
