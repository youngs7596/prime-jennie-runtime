"""Scout market_data loader — daily_prices × universe × lookback → list[dict].

Screening Executor (subprocess/docker backend) 은 stdin JSON payload 로 데이터를
받기 때문에 DataFrame 자체를 넘길 수 없다. Host 에서 dict records 로 직렬화 → executor
`_stdio_main` 에서 pandas.DataFrame 으로 복원한다.

SQL: LATERAL 서브쿼리로 universe 종목마다 최근 lookback_days 건만 뽑는다. 전체
daily_prices 를 긁고 slicing 하는 것보다 lookback 이 작을수록 효율적이다.

daily_prices 스키마 — migrations/003_price_tables.sql 참조:
    stock_code TEXT, price_date DATE, open/high/low/close_price INT, volume BIGINT
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def load_market_data_records(
    engine: AsyncEngine | None,
    *,
    universe: list[str],
    as_of: date,
    lookback_days: int = 60,
) -> list[dict[str, Any]]:
    """daily_prices × universe × lookback 을 JSON 직렬화 가능한 records 로 반환.

    각 record:
        {"ticker": str, "date": "YYYY-MM-DD", "open": int, "high": int,
         "low": int, "close": int, "volume": int}

    engine=None 이거나 universe 가 비어있으면 빈 리스트 반환 (smoke 환경 호환).
    """
    if engine is None or not universe:
        return []

    stmt = text(
        """
        SELECT
            sub.stock_code AS ticker,
            sub.price_date AS price_date,
            sub.open_price AS open,
            sub.high_price AS high,
            sub.low_price AS low,
            sub.close_price AS close,
            sub.volume AS volume
        FROM unnest(CAST(:tickers AS TEXT[])) AS t(stock_code)
        CROSS JOIN LATERAL (
            SELECT stock_code, price_date, open_price, high_price,
                   low_price, close_price, volume
            FROM daily_prices dp
            WHERE dp.stock_code = t.stock_code
              AND dp.price_date <= :as_of
            ORDER BY dp.price_date DESC
            LIMIT :lookback
        ) AS sub
        ORDER BY sub.stock_code, sub.price_date
        """
    )

    async with engine.connect() as conn:
        res = await conn.execute(
            stmt,
            {"tickers": list(universe), "as_of": as_of, "lookback": int(lookback_days)},
        )
        rows = res.mappings().all()

    records: list[dict[str, Any]] = []
    for row in rows:
        records.append(
            {
                "ticker": row["ticker"],
                "date": row["price_date"].isoformat(),
                "open": int(row["open"]),
                "high": int(row["high"]),
                "low": int(row["low"]),
                "close": int(row["close"]),
                "volume": int(row["volume"]),
            }
        )

    logger.info(
        "Loaded %d market_data rows (universe=%d, lookback=%d, as_of=%s)",
        len(records),
        len(universe),
        lookback_days,
        as_of.isoformat(),
    )
    return records
