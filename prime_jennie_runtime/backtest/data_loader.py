"""v3 `daily_prices` 를 `DailyBar` 시퀀스로 로드.

v2 adapters (legacy_quant_scores, macro_days, watchlists) 는 전부 제거. v3 는
PositionSheet JSON 이 이미 시점 컨텍스트를 담고 있으므로 백테스트에 필요한
외부 입력은 일봉 OHLCV 뿐이다.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .domain import DailyBar

logger = logging.getLogger(__name__)


async def load_daily_bars(
    engine: AsyncEngine,
    ticker: str,
    *,
    start: date,
    end: date,
) -> list[DailyBar]:
    """특정 ticker 의 [start, end] 범위 일봉을 오름차순으로 반환."""
    query = text(
        """
        SELECT price_date, open_price, high_price, low_price, close_price, volume
        FROM daily_prices
        WHERE stock_code = :ticker
          AND price_date BETWEEN :start AND :end
        ORDER BY price_date ASC
        """
    )
    async with engine.connect() as conn:
        result = await conn.execute(
            query,
            {"ticker": ticker, "start": start, "end": end},
        )
        rows = result.mappings().all()

    bars = [
        DailyBar(
            ticker=ticker,
            price_date=row["price_date"],
            open=float(row["open_price"]),
            high=float(row["high_price"]),
            low=float(row["low_price"]),
            close=float(row["close_price"]),
            volume=int(row["volume"] or 0),
        )
        for row in rows
    ]
    if not bars:
        logger.warning("load_daily_bars: no bars for %s in [%s, %s]", ticker, start, end)
    return bars


__all__ = ["load_daily_bars"]
