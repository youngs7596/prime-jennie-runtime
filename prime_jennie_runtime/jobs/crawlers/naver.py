"""네이버 금융 크롤러 — 종목 fundamentals/ROE/섹터 매핑.

v2 `prime_jennie/infra/crawlers/naver.py` 에서 옮겨온 함수들이다. 값의 의미
(최근 실적 분기 PER/PBR/ROE, ROE 행의 마지막 유효값, 업종 79개 순회)는 v2 부터
그대로 유지하고, **2026-09-15 에 읽는 곳만 HTML 페이지에서 모바일 JSON API 로
바꿨다**. 네이버가 `finance.naver.com` 을 `stock.naver.com` 으로 옮겨 옛 주소가
302 만 돌려주게 됐기 때문이다 (`naver_api` 모듈 설명 참고).

- `crawl_naver_fundamentals(client, stock_code)` : 분기 재무표에서 추정치가 아닌
  가장 최근 분기의 PER/PBR/ROE
- `crawl_naver_roe(client, stock_code)` : 분기 재무표 ROE 행의 마지막 유효값
  (추정 분기 포함 — fundamentals 쪽과 일부러 다른 값을 봐서 서로 교차검증이 된다)
- `build_naver_sector_mapping(client)` : 네이버 업종 분류 → {code: sector_name}

뉴스 크롤은 `news_pipeline_kor/adapters/naver_crawler.py` 에 따로 있다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .naver_api import NAVER_HEADERS, get_json, parse_number

logger = logging.getLogger(__name__)

__all__ = [
    "NAVER_HEADERS",
    "NaverFundamentals",
    "build_naver_sector_mapping",
    "crawl_naver_fundamentals",
    "crawl_naver_roe",
]

_INDUSTRY_PAGE_SIZE = 100


@dataclass(frozen=True)
class NaverFundamentals:
    """분기 재무표에서 뽑은 최신 실적 분기 지표."""

    per: float | None = None
    pbr: float | None = None
    roe: float | None = None
    quarter_name: str | None = None


@dataclass(frozen=True)
class _QuarterFinance:
    """분기 재무표 — 기간 목록과 지표 행."""

    periods: list[tuple[str, str, bool]]  # (기간키, 표시이름, 추정치 여부) 과거→최근
    rows: dict[str, dict[str, Any]]  # 지표명 → {기간키: {"value": ...}}

    def value(self, metric: str, period_key: str) -> float | None:
        cell = self.rows.get(metric, {}).get(period_key)
        if not isinstance(cell, dict):
            return None
        return parse_number(cell.get("value"))


async def _fetch_quarter_finance(
    client: httpx.AsyncClient, stock_code: str
) -> _QuarterFinance | None:
    """종목의 분기 재무표를 읽는다.

    응답 모양: `financeInfo.trTitleList` 가 기간 목록(`isConsensus` 가 "Y" 면
    추정치), `financeInfo.rowList` 가 지표 행(`title` = PER/PBR/ROE/EPS/BPS,
    `columns` = {기간키: {"value": "14.98"}}).
    """
    data = await get_json(client, f"/stock/{stock_code}/finance/quarter")
    if not isinstance(data, dict):
        return None
    info = data.get("financeInfo")
    if not isinstance(info, dict):
        return None

    periods: list[tuple[str, str, bool]] = []
    for entry in info.get("trTitleList") or []:
        key = entry.get("key")
        if not key:
            continue
        title = str(entry.get("title") or key).rstrip(".")
        periods.append((str(key), title, entry.get("isConsensus") == "Y"))
    if not periods:
        return None
    periods.sort(key=lambda p: p[0])

    rows: dict[str, dict[str, Any]] = {}
    for row in info.get("rowList") or []:
        title = row.get("title")
        columns = row.get("columns")
        if isinstance(title, str) and isinstance(columns, dict):
            rows[title.strip()] = columns
    if not rows:
        return None

    return _QuarterFinance(periods=periods, rows=rows)


async def crawl_naver_roe(client: httpx.AsyncClient, stock_code: str) -> float | None:
    """ROE(%) — 분기 재무표 ROE 행의 마지막 유효값.

    v2 규칙 유지: 추정 분기도 후보에 넣고, 값이 비어 있으면("-") 건너뛰며 가장
    오른쪽(최근) 유효값을 쓴다.
    """
    finance = await _fetch_quarter_finance(client, stock_code)
    if finance is None:
        return None

    best: float | None = None
    for key, _title, _is_estimate in finance.periods:
        value = finance.value("ROE", key)
        if value is not None:
            best = value
    return best


async def crawl_naver_fundamentals(
    client: httpx.AsyncClient, stock_code: str
) -> NaverFundamentals | None:
    """추정치가 아닌 가장 최근 분기의 PER/PBR/ROE.

    v2 규칙 유지: 기간을 오른쪽(최근)부터 훑어 추정 분기를 건너뛴 첫 기간을 고르고,
    그 한 기간의 값만 읽는다. 세 값이 전부 비면 None 을 돌려줘 호출측이 실패로 센다.
    """
    finance = await _fetch_quarter_finance(client, stock_code)
    if finance is None:
        return None

    actual = [(key, title) for key, title, is_estimate in finance.periods if not is_estimate]
    if not actual:
        return None
    period_key, quarter_name = actual[-1]

    per = finance.value("PER", period_key)
    pbr = finance.value("PBR", period_key)
    roe = finance.value("ROE", period_key)
    if per is None and pbr is None and roe is None:
        return None

    return NaverFundamentals(per=per, pbr=pbr, roe=roe, quarter_name=quarter_name)


async def _get_sector_stocks(client: httpx.AsyncClient, sector_no: str) -> list[str]:
    """업종 하나에 속한 종목 코드 전부 (코스피·코스닥 섞여 나온다 — v2 와 동일)."""
    codes: list[str] = []
    page = 1
    while True:
        data = await get_json(
            client,
            f"/stocks/industry/{sector_no}",
            params={"page": page, "pageSize": _INDUSTRY_PAGE_SIZE},
        )
        if not isinstance(data, dict):
            break
        stocks = data.get("stocks") or []
        for stock in stocks:
            code = str(stock.get("itemCode") or "")
            if len(code) == 6 and code.isdigit():
                codes.append(code)
        total = data.get("totalCount")
        if len(stocks) < _INDUSTRY_PAGE_SIZE:
            break
        if isinstance(total, int) and page * _INDUSTRY_PAGE_SIZE >= total:
            break
        page += 1
    return codes


async def build_naver_sector_mapping(
    client: httpx.AsyncClient, *, request_delay: float = 0.2
) -> dict[str, str]:
    """네이버 업종 분류 → {stock_code: sector_name}. v2 와 같이 79개 세분류 순회."""
    mapping: dict[str, str] = {}
    data = await get_json(
        client,
        "/stocks/industry",
        params={"page": 1, "pageSize": _INDUSTRY_PAGE_SIZE},
        timeout=15.0,
    )
    if not isinstance(data, dict):
        return mapping

    for group in data.get("groups") or []:
        sector_no = group.get("no")
        sector_name = group.get("name")
        if sector_no is None or not sector_name:
            continue
        for code in await _get_sector_stocks(client, str(sector_no)):
            mapping[code] = str(sector_name)
        await asyncio.sleep(request_delay)

    logger.info("Naver sector mapping: %d stocks mapped", len(mapping))
    return mapping
