"""온통청년 정책 코드표.

출처: 온통청년 정책 검색 화면(youthPolicy/ythPlcyTotalSearch)의 필터 값·표시 로직.
Open API 문서(getPlcy)에는 코드 값 목록이 없다.
상세는 docs/journal/2026-10-17-public-api-discovery.md.
"""

MARRIAGE: dict[str, str] = {
    "0055001": "기혼",
    "0055002": "미혼",
    "0055003": "제한없음",
}
MARRIAGE_ANY = "0055003"

# 0043003 은 화면에서 이름 없이 earnEtcCn 자유 텍스트로만 표시된다.
EARN_CONDITION: dict[str, str] = {
    "0043001": "무관",
    "0043002": "연소득 범위",
    "0043003": "기타",
}
EARN_ANY = "0043001"
EARN_RANGE = "0043002"

JOB: dict[str, str] = {
    "0013001": "재직자",
    "0013002": "자영업자",
    "0013003": "미취업자",
    "0013004": "프리랜서",
    "0013005": "일용근로자",
    "0013006": "(예비)창업자",
    "0013007": "단기근로자",
    "0013008": "영농종사자",
    "0013009": "기타",
    "0013010": "제한없음",
}
JOB_ANY = "0013010"

SCHOOL: dict[str, str] = {
    "0049001": "고졸 미만",
    "0049002": "고교 재학",
    "0049003": "고졸 예정",
    "0049004": "고교 졸업",
    "0049005": "대학 재학",
    "0049006": "대졸 예정",
    "0049007": "대학 졸업",
    "0049008": "석·박사",
    "0049009": "기타",
    "0049010": "제한없음",
}
SCHOOL_ANY = "0049010"

MAJOR: dict[str, str] = {
    "0011001": "인문계열",
    "0011002": "사회계열",
    "0011003": "상경계열",
    "0011004": "이학계열",
    "0011005": "공학계열",
    "0011006": "예체능계열",
    "0011007": "농산업계열",
    "0011008": "기타",
    "0011009": "제한없음",
}
MAJOR_ANY = "0011009"

SPECIAL: dict[str, str] = {
    "0014001": "중소기업",
    "0014002": "여성",
    "0014003": "기초생활수급자",
    "0014004": "한부모가정",
    "0014005": "장애인",
    "0014006": "농업인",
    "0014007": "군인",
    "0014008": "지역인재",
    "0014009": "기타",
    "0014010": "제한없음",
}
SPECIAL_ANY = "0014010"

APPLY_PERIOD: dict[str, str] = {
    "0057001": "특정 기간",
    "0057002": "상시",
}
APPLY_PERIOD_FIXED = "0057001"
APPLY_PERIOD_ALWAYS = "0057002"

# sprtTrgtAgeLmtYn 은 이름과 반대로 'Y' 가 연령 제한 없음이다 (공식 화면 표시 로직 기준).
AGE_NO_LIMIT_FLAG = "Y"

# 정규화된 Policy 컬럼 → 해당 코드표. 미등록 코드 집계에 쓴다.
POLICY_CODE_COLUMNS: dict[str, dict[str, str]] = {
    "marriage_code": MARRIAGE,
    "earn_cond_code": EARN_CONDITION,
    "job_codes": JOB,
    "school_codes": SCHOOL,
    "major_codes": MAJOR,
    "special_codes": SPECIAL,
    "apply_period_code": APPLY_PERIOD,
}
