"""금감원 금융상품통합비교공시 API 클라이언트 (적금 상품, 금융회사)."""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.ingest.http import ApiHttpClient, IngestAPIError

logger = logging.getLogger(__name__)

FSS_BASE_URL = "https://finlife.fss.or.kr/finlifeapi"
SAVING_PRODUCTS_URL = f"{FSS_BASE_URL}/savingProductsSearch.json"
COMPANY_URL = f"{FSS_BASE_URL}/companySearch.json"

FIN_GRP_BANK = "020000"
FIN_GRP_SAVINGS_BANK = "030300"
DEFAULT_FIN_GROUPS = (FIN_GRP_BANK, FIN_GRP_SAVINGS_BANK)

FSS_OK = "000"


@dataclass
class FssListing:
    """한 권역(topFinGrpNo)의 전 페이지 baseList/optionList 를 합친 결과.

    응답 항목에는 권역 코드가 없으므로 `fin_grp_code` 로 함께 전달한다.
    """

    fin_grp_code: str
    base_list: list[dict[str, Any]] = field(default_factory=list)
    option_list: list[dict[str, Any]] = field(default_factory=list)
    total_count: int = 0


class FssClient:
    def __init__(self, api_key: str, http: ApiHttpClient) -> None:
        if not api_key:
            raise ValueError("FSS_API_KEY 가 비어 있습니다")
        self._api_key = api_key
        self._http = http

    @classmethod
    def from_settings(cls, settings: Settings, http: ApiHttpClient) -> "FssClient":
        if not settings.fss_api_key:
            raise ValueError(".env 에 FSS_API_KEY 를 설정하세요")
        return cls(settings.fss_api_key, http)

    async def _fetch_page(self, url: str, fin_grp_code: str, page_no: int) -> dict[str, Any]:
        body = await self._http.get_json(
            url,
            {"auth": self._api_key, "topFinGrpNo": fin_grp_code, "pageNo": page_no},
            secret_params=("auth",),
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise IngestAPIError(f"금감원 응답 형식 오류: {url}")
        err_cd = result.get("err_cd")
        if err_cd != FSS_OK:
            raise IngestAPIError(f"금감원 오류 응답: err_cd={err_cd} {result.get('err_msg') or ''}")
        return result

    async def _fetch_listing(self, url: str, fin_grp_code: str) -> FssListing:
        listing = FssListing(fin_grp_code=fin_grp_code)
        page_no, max_page_no = 1, 1
        while page_no <= max_page_no:
            result = await self._fetch_page(url, fin_grp_code, page_no)
            if page_no == 1:
                max_page_no = int(result.get("max_page_no") or 1)
                listing.total_count = int(result.get("total_count") or 0)
            listing.base_list.extend(result.get("baseList") or [])
            listing.option_list.extend(result.get("optionList") or [])
            page_no += 1
        logger.info(
            "금감원 %s (권역 %s): %d건, %d페이지",
            url.rsplit("/", 1)[-1],
            fin_grp_code,
            len(listing.base_list),
            max_page_no,
        )
        return listing

    async def fetch_saving_products(self, fin_grp_code: str) -> FssListing:
        """baseList = 상품, optionList = 기간·적립방식별 금리."""
        return await self._fetch_listing(SAVING_PRODUCTS_URL, fin_grp_code)

    async def fetch_companies(self, fin_grp_code: str) -> FssListing:
        """baseList = 금융회사(homp_url 포함), optionList = 영업 지역."""
        return await self._fetch_listing(COMPANY_URL, fin_grp_code)
