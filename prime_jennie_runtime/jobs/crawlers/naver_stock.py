"""네이버 금융 종목별 외국인/기관 수급 크롤러.

v2 원본: `prime_jennie/infra/crawlers/naver_stock.py`. 값의 의미(일자별 종가 +
기관·외국인 순매매량 + 외국인 보유율)는 v2 부터 그대로이고, **2026-09-15 에 읽는
곳만 옛 `frgn.naver` HTML 표에서 모바일 JSON API 로 바꿨다**. 네이버 사이트 이전
사정은 `naver_api` 모듈 설명 참고.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from .naver_api import NAVER_HEADERS, get_json, parse_number

logger = logging.getLogger(__name__)

# 옛 frgn.naver 1페이지가 담던 일수와 맞춘다 — 호출측이 최근 7거래일만 쓴다.
DEFAULT_TREND_DAYS = 20


@dataclass
class StockFrgnRow:
    """종목별 외국인/기관 일별 수급 데이터."""

    trade_date: date
    close_price: int  # 종가 (원)
    inst_net_volume: int  # 기관 순매매량 (주)
    frgn_net_volume: int  # 외국인 순매매량 (주)
    frgn_holding_ratio: float  # 외국인 보유율 (%)


def _to_int(raw: Any) -> int:
    value = parse_number(raw)
    return 0 if value is None else int(value)


def parse_trend_rows(payload: Any) -> list[StockFrgnRow]:
    """`/stock/{code}/trend` 응답(일자별 수급 배열)을 StockFrgnRow 목록으로.

    한 행 모양: `{"bizdate": "20260915", "closePrice": "250,500",
    "foreignerPureBuyQuant": "-1,088,039", "organPureBuyQuant": "-2,321,959",
    "foreignerHoldRatio": "46.55%"}`. 날짜가 없거나 형식이 깨진 행은 버린다.
    """
    if not isinstance(payload, list):
        return []

    rows: list[StockFrgnRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_date = str(item.get("bizdate") or "").strip()
        try:
            trade_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue

        ratio = parse_number(item.get("foreignerHoldRatio"))
        rows.append(
            StockFrgnRow(
                trade_date=trade_date,
                close_price=_to_int(item.get("closePrice")),
                inst_net_volume=_to_int(item.get("organPureBuyQuant")),
                frgn_net_volume=_to_int(item.get("foreignerPureBuyQuant")),
                frgn_holding_ratio=0.0 if ratio is None else ratio,
            )
        )
    return rows


async def fetch_stock_frgn_data(
    client: httpx.AsyncClient, stock_code: str, *, days: int = DEFAULT_TREND_DAYS
) -> list[StockFrgnRow] | None:
    """종목 하나의 최근 일별 수급. 실패(네트워크·파싱 모두) 시 None."""
    payload = await get_json(
        client,
        f"/stock/{stock_code}/trend",
        params={"page": 1, "pageSize": days},
    )
    if payload is None:
        return None

    rows = parse_trend_rows(payload)
    if not rows:
        logger.warning("[%s] no frgn data rows parsed", stock_code)
        return None
    return rows


__all__ = [
    "DEFAULT_TREND_DAYS",
    "NAVER_HEADERS",
    "StockFrgnRow",
    "fetch_stock_frgn_data",
    "parse_trend_rows",
]
