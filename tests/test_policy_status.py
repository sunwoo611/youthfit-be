from datetime import date

import pytest

from app.ingest import codes
from app.services.policy_status import Availability, policy_availability

TODAY = date(2026, 10, 26)
PAST = date(2026, 10, 25)
FUTURE = date(2026, 10, 27)

FIXED = codes.APPLY_PERIOD_FIXED
ALWAYS = codes.APPLY_PERIOD_ALWAYS
UNSPECIFIED = codes.APPLY_PERIOD_UNSPECIFIED


def _status(code: str | None, **dates: date | None) -> Availability:
    kwargs = {"apply_start": None, "apply_end": None, "biz_start": None, "biz_end": None}
    kwargs.update(dates)
    return policy_availability(code, today=TODAY, **kwargs)


@pytest.mark.parametrize(
    ("code", "dates", "expected"),
    [
        # 특정 기간: 신청 기간으로만 판단한다
        (FIXED, {"apply_start": PAST, "apply_end": TODAY}, Availability.OPEN),
        (FIXED, {"apply_start": PAST, "apply_end": PAST}, Availability.CLOSED),
        (FIXED, {"apply_start": FUTURE, "apply_end": FUTURE}, Availability.UPCOMING),
        (FIXED, {"apply_end": FUTURE, "biz_start": FUTURE}, Availability.OPEN),
        (FIXED, {}, Availability.UNVERIFIED),
        # 상시: 사업이 끝났거나 시작 전이 아니면 신청 가능
        (ALWAYS, {}, Availability.OPEN),
        (ALWAYS, {"biz_end": TODAY}, Availability.OPEN),
        (ALWAYS, {"biz_end": PAST}, Availability.CLOSED),
        (ALWAYS, {"biz_start": FUTURE}, Availability.UPCOMING),
        # 미지정: 사업이 끝난 것만 확정하고 나머지는 확인 필요
        (UNSPECIFIED, {"biz_end": PAST}, Availability.CLOSED),
        (UNSPECIFIED, {"biz_start": FUTURE}, Availability.UPCOMING),
        (UNSPECIFIED, {"biz_end": FUTURE}, Availability.UNVERIFIED),
        (UNSPECIFIED, {}, Availability.UNVERIFIED),
        (None, {}, Availability.UNVERIFIED),
        # 사업 종료가 신청 기간보다 앞서면 마감으로 본다
        (FIXED, {"apply_end": FUTURE, "biz_end": PAST}, Availability.CLOSED),
    ],
)
def test_policy_availability(code, dates, expected):
    assert _status(code, **dates) is expected
