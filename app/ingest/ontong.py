"""온통청년 청년정책 API (getPlcy) 클라이언트."""

import logging
import math
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.ingest.http import ApiHttpClient, IngestAPIError

logger = logging.getLogger(__name__)

ONTONG_POLICY_URL = "https://www.youthcenter.go.kr/go/ythip/getPlcy"
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class OntongPage:
    page_num: int
    total_count: int
    policies: list[dict[str, Any]]


@dataclass(frozen=True)
class OntongFetchResult:
    total_count: int
    policies: list[dict[str, Any]]


class OntongClient:
    def __init__(
        self,
        api_key: str,
        http: ApiHttpClient,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if not api_key:
            raise ValueError("ONTONG_YOUTH_API_KEY 가 비어 있습니다")
        self._api_key = api_key
        self._http = http
        self._page_size = page_size

    @classmethod
    def from_settings(cls, settings: Settings, http: ApiHttpClient) -> "OntongClient":
        if not settings.ontong_youth_api_key:
            raise ValueError(".env 에 ONTONG_YOUTH_API_KEY 를 설정하세요")
        return cls(settings.ontong_youth_api_key, http)

    async def fetch_page(self, page_num: int) -> OntongPage:
        body = await self._http.get_json(
            ONTONG_POLICY_URL,
            {
                "apiKeyNm": self._api_key,
                "pageNum": page_num,
                "pageSize": self._page_size,
                "rtnType": "json",
            },
            secret_params=("apiKeyNm",),
        )
        if not isinstance(body, dict) or body.get("resultCode") != 200:
            code = body.get("resultCode") if isinstance(body, dict) else None
            message = body.get("resultMessage") if isinstance(body, dict) else None
            raise IngestAPIError(f"온통청년 오류 응답: resultCode={code} {message or ''}".rstrip())

        result = body.get("result") or {}
        paging = result.get("pagging") or {}
        return OntongPage(
            page_num=page_num,
            total_count=int(paging.get("totCount") or 0),
            policies=list(result.get("youthPolicyList") or []),
        )

    async def fetch_all_policies(self) -> OntongFetchResult:
        """1페이지의 totCount 로 마지막 페이지를 정하고, 빈 페이지가 오면 조기 종료한다.

        페이지 사이에 데이터가 바뀌면 중복·누락이 생길 수 있어 중복은 그대로 돌려준다.
        """
        first = await self.fetch_page(1)
        last_page = max(1, math.ceil(first.total_count / self._page_size))
        logger.info("온통청년 총 %d건, %d페이지", first.total_count, last_page)
        policies = list(first.policies)

        for page_num in range(2, last_page + 1):
            page = await self.fetch_page(page_num)
            if not page.policies:
                logger.warning("온통청년 %d페이지가 비어 있어 수집을 종료합니다", page_num)
                break
            policies.extend(page.policies)

        return OntongFetchResult(total_count=first.total_count, policies=policies)
