"""미국 시장 일봉 크롤러 — Yahoo Finance chart API.

v2 원본: `prime_jennie/infra/crawlers/us_market.py`. v3 어댑터 차이:
- sync httpx.get → async `client.get` (호출측 `AsyncClient` 주입)
- 공급 티커 목록 / Yahoo range 매핑 / prev_close 기반 change_pct 등 로직은 동일
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

logger = logging.getLogger(__name__)


# 수집 대상 티커 (Yahoo ticker → 정규화 이름).
US_TICKERS: dict[str, str] = {
    "^SOX": "SOX",
    "NVDA": "NVDA",
    "^GSPC": "SP500",
    "^IXIC": "NASDAQ",
    "NQ=F": "NQ_FUT",
    "BZ=F": "BRENT",  # 브렌트유 — 코스피 유가 충격 팩터 (2026-06-09 추가, 한국 수입원유 벤치마크)
}

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}


@dataclass
class USMarketDaily:
    ticker: str
    price_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    change_pct: float | None = None


def _range_for_days(days: int) -> str:
    if days <= 30:
        return "1mo"
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    if days <= 365:
        return "1y"
    return "2y"


async def fetch_us_daily(
    client: httpx.AsyncClient, yahoo_ticker: str, days: int = 500
) -> list[USMarketDaily]:
    """단일 티커의 Yahoo 일봉. 오래된 순 정렬. 실패 시 빈 리스트."""
    ticker_name = US_TICKERS.get(yahoo_ticker, yahoo_ticker)
    yrange = _range_for_days(days)
    try:
        resp = await client.get(
            _YAHOO_CHART_URL.format(ticker=yahoo_ticker),
            params={"range": yrange, "interval": "1d"},
            headers=_YAHOO_HEADERS,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()

        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        opens = quote["open"]
        highs = quote["high"]
        lows = quote["low"]
        closes = quote["close"]
        volumes = quote["volume"]

        rows: list[USMarketDaily] = []
        prev_close: float | None = None
        for i, ts in enumerate(timestamps):
            o, h, lo, c, v = opens[i], highs[i], lows[i], closes[i], volumes[i]
            if c is None:
                continue
            price_date = datetime.fromtimestamp(ts, tz=UTC).date()
            change_pct: float | None = None
            if prev_close and prev_close > 0:
                change_pct = round((c - prev_close) / prev_close * 100, 4)
            rows.append(
                USMarketDaily(
                    ticker=ticker_name,
                    price_date=price_date,
                    open_price=round(o, 2) if o else 0.0,
                    high_price=round(h, 2) if h else 0.0,
                    low_price=round(lo, 2) if lo else 0.0,
                    close_price=round(c, 2),
                    volume=int(v) if v else 0,
                    change_pct=change_pct,
                )
            )
            prev_close = c
        logger.info("yahoo %s: %d daily rows (range=%s)", ticker_name, len(rows), yrange)
        return rows
    except Exception as e:
        logger.warning("yahoo %s fetch failed: %s", ticker_name, e)
        return []


async def fetch_us_market_batch(
    client: httpx.AsyncClient, days: int = 500
) -> dict[str, list[USMarketDaily]]:
    """`US_TICKERS` 전체를 순차 수집 (v2 와 동일한 순서)."""
    result: dict[str, list[USMarketDaily]] = {}
    for yahoo_ticker, ticker_name in US_TICKERS.items():
        rows = await fetch_us_daily(client, yahoo_ticker, days=days)
        if rows:
            result[ticker_name] = rows
    return result


__all__ = [
    "US_TICKERS",
    "USMarketDaily",
    "fetch_us_daily",
    "fetch_us_market_batch",
]
