"""DART OpenAPI list.json 클라이언트 — async httpx.

v2 는 OpenDartReader (sync, pandas) 를 썼지만 v3 는 직접 list.json 을 호출해 의존성을
줄인다. 응답 스펙은 OpenDartReader 의 list() 결과와 동등 (필요 컬럼만).

Docs: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_PAGE_COUNT = 100  # API max
DART_MAX_PAGES = 50  # 안전 상한 (5,000 row). 운영상 일주일치 정기공시 < 1,500.


@dataclass
class DartFiling:
    """DART list.json row — 필요한 필드만."""

    corp_code: str  # 8자리 DART 고유 코드
    stock_code: str | None  # 6자리 종목코드 (비상장은 None/빈문자)
    corp_name: str
    report_nm: str  # 공시명
    rcept_no: str  # 접수번호 (UNIQUE)
    rcept_dt: str  # 접수일 (yyyymmdd)
    pblntf_ty: str  # 공시 유형 (A=정기, B=주요사항, ...)


async def fetch_dart_filings(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    bgn_de: str,
    end_de: str,
    pblntf_ty: str = "A",
    max_pages: int = DART_MAX_PAGES,
) -> list[DartFiling]:
    """DART list.json 페이지네이션 수집.

    bgn_de/end_de 는 yyyymmdd (v2 OpenDartReader.list 는 ISO 를 받지만 OpenAPI 는
    yyyymmdd). pblntf_ty="A" 는 v2 와 동일 (정기공시).
    실패/비정상 상태 코드는 빈 리스트 + 경고 로그.
    """
    filings: list[DartFiling] = []
    for page in range(1, max_pages + 1):
        params = {
            "crtfc_key": api_key,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": pblntf_ty,
            "page_no": str(page),
            "page_count": str(DART_PAGE_COUNT),
        }
        try:
            resp = await client.get(DART_LIST_URL, params=params, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("dart list page=%d failed: %s", page, e)
            return filings

        status = str(data.get("status", ""))
        # "000" = OK, "013" = 조회 결과 없음 (일/주말 케이스)
        if status == "013":
            return filings
        if status != "000":
            logger.warning(
                "dart list status=%s msg=%s page=%d",
                status,
                data.get("message"),
                page,
            )
            return filings

        rows = data.get("list", []) or []
        for row in rows:
            filings.append(
                DartFiling(
                    corp_code=str(row.get("corp_code", "")),
                    stock_code=str(row.get("stock_code", "")).strip() or None,
                    corp_name=str(row.get("corp_name", "")),
                    report_nm=str(row.get("report_nm", "")),
                    rcept_no=str(row.get("rcept_no", "")),
                    rcept_dt=str(row.get("rcept_dt", "")),
                    pblntf_ty=str(row.get("pblntf_ty", "")),
                )
            )

        total_page = int(data.get("total_page", 1) or 1)
        if page >= total_page:
            return filings

    logger.warning("dart list hit max_pages=%d cap", max_pages)
    return filings


__all__ = [
    "DART_LIST_URL",
    "DART_MAX_PAGES",
    "DART_PAGE_COUNT",
    "DartFiling",
    "fetch_dart_filings",
]
