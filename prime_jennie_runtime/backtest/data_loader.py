"""v3 Postgres → 백테스트 메모리 데이터 로더.

포팅 원본: prime-jennie/prime_jennie/services/backtest/data_loader.py
v2 는 MariaDB sync SQLModel 세션, v3 는 asyncpg / SQLAlchemy async engine.
테이블 매핑은 AGENTS.md 참조.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .domain import MarketRegime, TradeTier
from .models import DailyOHLCV, MacroDay, PriceCache, WatchlistEntry

logger = logging.getLogger(__name__)


def _score_to_regime(score: float | int | None) -> MarketRegime:
    """Sentiment score → MarketRegime (v2 jobs/app.py _update_trading_context 와 동일)."""
    if score is None:
        return MarketRegime.SIDEWAYS
    s = float(score)
    if s >= 70:
        return MarketRegime.STRONG_BULL
    if s >= 60:
        return MarketRegime.BULL
    if s >= 40:
        return MarketRegime.SIDEWAYS
    if s >= 25:
        return MarketRegime.BEAR
    return MarketRegime.STRONG_BEAR


async def load_prices(
    engine: AsyncEngine,
    start_date: date,
    end_date: date,
    buffer_days: int = 60,
) -> PriceCache:
    """v3 `daily_prices` 일괄 로드.

    buffer_days: start_date 이전 데이터도 로드 (ATR, RSI 등 기술 지표용).
    """
    buffered_start = start_date - timedelta(days=buffer_days)

    stmt = text(
        """
        SELECT stock_code, price_date, open_price, high_price, low_price, close_price, volume
        FROM daily_prices
        WHERE price_date >= :start AND price_date <= :end
        ORDER BY stock_code, price_date
        """
    )

    async with engine.connect() as conn:
        rows = (
            (await conn.execute(stmt, {"start": buffered_start, "end": end_date})).mappings().all()
        )

    cache = PriceCache()
    for row in rows:
        ohlcv = DailyOHLCV(
            price_date=row["price_date"],
            open_price=int(row["open_price"]),
            high_price=int(row["high_price"]),
            low_price=int(row["low_price"]),
            close_price=int(row["close_price"]),
            volume=int(row["volume"]),
        )
        cache.by_stock_date.setdefault(row["stock_code"], {})[row["price_date"]] = ohlcv
        cache.by_stock_sorted.setdefault(row["stock_code"], []).append(ohlcv)

    logger.info(
        "Loaded %d price rows for %d stocks (buffer=%d days)",
        len(rows),
        len(cache.by_stock_date),
        buffer_days,
    )
    return cache


async def load_watchlists(
    engine: AsyncEngine,
    start_date: date,
    end_date: date,
) -> dict[date, list[WatchlistEntry]]:
    """v3 `legacy_quant_scores` 의 is_final_selected 항목을 일자별로 묶어 반환.

    v2 `watchlist_histories` 는 v3 에 없음. 대신 v2 alembic 대응 테이블인 legacy
    을 사용. sector_group 은 v3 에 stock_masters 가 없어 None 으로 둔다.
    """
    stmt = text(
        """
        SELECT score_date, stock_code, stock_name, hybrid_score, llm_score,
               trade_tier, risk_tag
        FROM legacy_quant_scores
        WHERE score_date >= :start AND score_date <= :end
          AND is_final_selected = TRUE
          AND is_active = TRUE
          AND is_tradable = TRUE
        ORDER BY score_date, hybrid_score DESC NULLS LAST
        """
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt, {"start": start_date, "end": end_date})).mappings().all()

    result: dict[date, list[WatchlistEntry]] = defaultdict(list)
    for row in rows:
        tier_str = (row["trade_tier"] or "TIER2").upper()
        try:
            tier = TradeTier(tier_str)
        except ValueError:
            tier = TradeTier.TIER2
        if tier == TradeTier.BLOCKED:
            continue

        # 일자별 rank 는 해당 날짜 블록 내 순서로 부여
        rank = sum(1 for e in result[row["score_date"]]) + 1
        entry = WatchlistEntry(
            stock_code=row["stock_code"],
            stock_name=row["stock_name"],
            snapshot_date=row["score_date"],
            hybrid_score=float(row["hybrid_score"] or 0.0),
            llm_score=float(row["llm_score"] or 0.0),
            trade_tier=tier,
            risk_tag=row["risk_tag"] or "NEUTRAL",
            rank=rank,
            sector_group=None,  # v3 에 stock_masters 없음
        )
        result[row["score_date"]].append(entry)

    logger.info(
        "Loaded watchlists for %d dates, total %d entries",
        len(result),
        sum(len(v) for v in result.values()),
    )
    return dict(result)


async def load_macro_days(
    engine: AsyncEngine,
    start_date: date,
    end_date: date,
) -> dict[date, MacroDay]:
    """v3 `macro_runs` → 날짜별 MacroDay.

    v3 는 매크로 insight 의 세부 필드(sentiment_score/position_size_pct 등) 가 없어
    gate/size_multiplier 로부터 근사. 날짜별 최종 run 한 건만 사용.
    """
    stmt = text(
        """
        SELECT DATE(generated_at AT TIME ZONE 'Asia/Seoul') AS insight_date,
               gate, size_multiplier,
               ROW_NUMBER() OVER (
                   PARTITION BY DATE(generated_at AT TIME ZONE 'Asia/Seoul')
                   ORDER BY generated_at DESC
               ) AS rn
        FROM macro_runs
        WHERE generated_at >= :start AND generated_at < :end_plus_1
        """
    )
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    stmt,
                    {"start": start_date, "end_plus_1": end_date + timedelta(days=1)},
                )
            )
            .mappings()
            .all()
        )

    result: dict[date, MacroDay] = {}
    for row in rows:
        if row["rn"] != 1:
            continue
        # gate=open / closed + size_multiplier 로 regime 근사
        gate = row["gate"]
        mult = float(row["size_multiplier"] or 1.0)
        if gate == "closed":
            regime = MarketRegime.STRONG_BEAR
        elif mult >= 1.2:
            regime = MarketRegime.BULL
        elif mult <= 0.5:
            regime = MarketRegime.BEAR
        else:
            regime = MarketRegime.SIDEWAYS
        result[row["insight_date"]] = MacroDay(
            insight_date=row["insight_date"],
            sentiment=gate,
            regime=regime,
            position_size_pct=int(mult * 100),
            stop_loss_adjust_pct=100,
        )

    logger.info("Loaded macro insights for %d dates", len(result))
    return result


async def load_quant_scores(
    engine: AsyncEngine,
    start_date: date,
    end_date: date,
) -> dict[date, dict[str, dict[str, Any]]]:
    """v3 `legacy_quant_scores` → {score_date: {stock_code: row_dict}}.

    v2 `daily_quant_scores` 와 1:1 호환. Row 는 dict 로 반환 (ORM 없음).
    """
    stmt = text(
        """
        SELECT score_date, stock_code, stock_name,
               total_quant_score, momentum_score, quality_score, value_score,
               technical_score, news_score, supply_demand_score,
               llm_score, hybrid_score, llm_grade, llm_reason,
               risk_tag, trade_tier, is_tradable, is_final_selected, run_id
        FROM legacy_quant_scores
        WHERE score_date >= :start AND score_date <= :end
        ORDER BY score_date
        """
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt, {"start": start_date, "end": end_date})).mappings().all()

    result: dict[date, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        result[row["score_date"]][row["stock_code"]] = dict(row)

    logger.info(
        "Loaded quant scores for %d dates, total %d entries",
        len(result),
        sum(len(v) for v in result.values()),
    )
    return dict(result)


def get_trading_dates(price_cache: PriceCache, start_date: date, end_date: date) -> list[date]:
    """가격 데이터에서 실제 거래일 추출 (주말/공휴일 제외)."""
    all_dates: set[date] = set()
    for stock_dates in price_cache.by_stock_date.values():
        for d in stock_dates:
            if start_date <= d <= end_date:
                all_dates.add(d)
    return sorted(all_dates)


__all__ = [
    "get_trading_dates",
    "load_macro_days",
    "load_prices",
    "load_quant_scores",
    "load_watchlists",
]
