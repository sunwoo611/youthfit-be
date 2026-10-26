"""정책 신청 가능 여부. 기준일에 따라 달라지므로 적재 때 저장하지 않고 조회 시점에 판정한다."""

from datetime import date
from enum import StrEnum

from app.ingest import codes


class Availability(StrEnum):
    OPEN = "open"
    UPCOMING = "upcoming"
    CLOSED = "closed"
    # 신청기간 미지정(0057003) 등 원문만으로 지금 신청할 수 있는지 알 수 없음
    UNVERIFIED = "unverified"


def policy_availability(
    apply_period_code: str | None,
    *,
    apply_start: date | None,
    apply_end: date | None,
    biz_start: date | None,
    biz_end: date | None,
    today: date,
) -> Availability:
    if (apply_end and apply_end < today) or (biz_end and biz_end < today):
        return Availability.CLOSED

    if apply_period_code == codes.APPLY_PERIOD_FIXED:
        # 사업 시작 전에 모집하는 경우가 많아 특정 기간은 신청 기간만 본다
        if apply_end is None:
            return Availability.UNVERIFIED
        if apply_start and apply_start > today:
            return Availability.UPCOMING
        return Availability.OPEN

    if biz_start and biz_start > today:
        return Availability.UPCOMING
    if apply_period_code == codes.APPLY_PERIOD_ALWAYS:
        return Availability.OPEN
    return Availability.UNVERIFIED
