"""자연어 질의에서 지역·취업상태·나이·관심분야를 규칙 사전으로 보조 추출한다. LLM 은 쓰지 않는다."""

import re
from dataclasses import dataclass
from typing import TypeVar

from app.schemas.candidates import EmploymentStatus, Interest

_V = TypeVar("_V")

# "경기 광주"처럼 여러 지역이 나오면 문장에서 먼저 나온 것을 쓴다.
SIDO_KEYWORDS: dict[str, str] = {
    "서울": "11",
    "경기": "41",
    "인천": "28",
    "부산": "26",
    "대구": "27",
    "광주": "12",
    "전남": "12",
    "전라남도": "12",
    "대전": "30",
    "울산": "31",
    "세종": "36",
    "충북": "43",
    "충청북도": "43",
    "충남": "44",
    "충청남도": "44",
    "경북": "47",
    "경상북도": "47",
    "경남": "48",
    "경상남도": "48",
    "제주": "50",
    "강원": "51",
    "전북": "52",
    "전라북도": "52",
}

EMPLOYMENT_KEYWORDS: dict[str, EmploymentStatus] = {
    "취준": EmploymentStatus.UNEMPLOYED,
    "취업준비": EmploymentStatus.UNEMPLOYED,
    "취업 준비": EmploymentStatus.UNEMPLOYED,
    "구직": EmploymentStatus.UNEMPLOYED,
    "미취업": EmploymentStatus.UNEMPLOYED,
    "무직": EmploymentStatus.UNEMPLOYED,
    "백수": EmploymentStatus.UNEMPLOYED,
    "직장": EmploymentStatus.EMPLOYED,
    "재직": EmploymentStatus.EMPLOYED,
    "회사원": EmploymentStatus.EMPLOYED,
    "창업": EmploymentStatus.FOUNDER,
    "자영업": EmploymentStatus.SELF_EMPLOYED,
    "프리랜서": EmploymentStatus.FREELANCER,
    "영농": EmploymentStatus.FARMER,
    "귀농": EmploymentStatus.FARMER,
}

INTEREST_KEYWORDS: dict[Interest, tuple[str, ...]] = {
    Interest.HOUSING: ("월세", "전세", "주거", "주택", "자취", "임대"),
    Interest.JOBS: ("취업", "일자리", "구직", "채용", "인턴", "취준"),
    Interest.FINANCE: ("적금", "저축", "목돈", "자산", "대출", "금융"),
    Interest.EDUCATION: ("교육", "훈련", "자격증", "학자금", "강의"),
    Interest.WELFARE: ("복지", "건강", "문화", "생활비", "의료"),
    Interest.PARTICIPATION: ("참여", "봉사"),
}

_AGE = re.compile(r"(?:만\s*)?(?<!\d)(\d{2})\s*(?:살|세)")


@dataclass(frozen=True)
class QueryHints:
    age: int | None = None
    sido_code: str | None = None
    employment_status: EmploymentStatus | None = None
    interests: tuple[Interest, ...] = ()


def _first_match(text: str, table: dict[str, _V]) -> _V | None:
    hits = [(pos, value) for key, value in table.items() if (pos := text.find(key)) >= 0]
    return min(hits, key=lambda hit: hit[0])[1] if hits else None


def extract_hints(query: str | None) -> QueryHints:
    if not query or not query.strip():
        return QueryHints()
    age_match = _AGE.search(query)
    return QueryHints(
        age=int(age_match.group(1)) if age_match else None,
        sido_code=_first_match(query, SIDO_KEYWORDS),
        employment_status=_first_match(query, EMPLOYMENT_KEYWORDS),
        interests=tuple(
            interest
            for interest, words in INTEREST_KEYWORDS.items()
            if any(word in query for word in words)
        ),
    )
