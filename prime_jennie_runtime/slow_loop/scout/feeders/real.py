"""Scout feeder 실구현 — Postgres 기반 universe / news / sector / market.

Phase 2.12 Track D. `stub.py` 4종을 교체.

- RealUniverseFeeder: stock_masters 에서 market_cap 상위 N개 종목 코드 반환
  (N 은 env `SCOUT_UNIVERSE_SIZE`, default 200).
- RealNewsScoreFeeder: news_articles × news_sentiments 조인 → ticker 별 48h 내
  mean score + staleness_hours. 기사 0건 ticker 는 zero entry 로 포함 (Scout
  가 missing 보다 명시적 0건을 선호한다는 `NewsPipelineScoutFeeder` 컨벤션).
- RealSectorMomentumFeeder: daily_prices × stock_masters.sector_group 로 섹터별
  20 영업일 평균 수익률. daily_prices 가 비어 있으면 `{}` + WARNING log.
- RealMarketSummaryFeeder: index_daily_prices 에서 KOSPI/KOSDAQ 최신 close
  + change_pct. up/down_count 는 daily_prices 비어있으면 0 으로 fallback.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..schemas import MarketSummary, NewsScoreEntry

logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE_SIZE = 200
NEWS_LOOKBACK_HOURS = 48.0
SECTOR_MOMENTUM_LOOKBACK_DAYS = 20
SECTOR_MOMENTUM_FALLBACK_WINDOW_DAYS = 30


def _universe_size() -> int:
    raw = os.environ.get("SCOUT_UNIVERSE_SIZE")
    if not raw:
        return DEFAULT_UNIVERSE_SIZE
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "SCOUT_UNIVERSE_SIZE=%r 파싱 실패, default %d 사용", raw, DEFAULT_UNIVERSE_SIZE
        )
        return DEFAULT_UNIVERSE_SIZE
    return max(1, n)


# ---------------------------------------------------------------- Universe


class RealUniverseFeeder:
    """stock_masters 에서 market_cap 내림차순 상위 N 종목."""

    def __init__(self, engine: AsyncEngine, size: int | None = None) -> None:
        self._engine = engine
        self._size = size if size is not None else _universe_size()

    async def fetch(self, as_of: date) -> list[str]:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    "SELECT stock_code FROM stock_masters "
                    "WHERE is_active = TRUE AND market_cap IS NOT NULL "
                    "ORDER BY market_cap DESC LIMIT :n"
                ),
                {"n": self._size},
            )
            rows = res.all()
        return [r[0] for r in rows]


# ---------------------------------------------------------------- NewsScore


class RealNewsScoreFeeder:
    """news_events ticker 별 최근 48h 평균 sentiment_score + 메타데이터 집계.

    2026-04-21 전환: news_sentiments → news_events.
    확장: `high_impact_count` + `event_types` (ticker × event_type 집계) 를 Scout
    코드가 직접 조건식으로 쓸 수 있도록 실어준다.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        lookback_hours: float = NEWS_LOOKBACK_HOURS,
    ) -> None:
        self._engine = engine
        self._lookback_hours = lookback_hours

    async def fetch(self, as_of: date, universe: list[str]) -> dict[str, NewsScoreEntry]:
        if not universe:
            return {}
        now = datetime.combine(as_of, datetime.min.time()).replace(hour=23, minute=59, second=59)
        since = now - timedelta(hours=self._lookback_hours)

        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    "SELECT ticker, "
                    "AVG(sentiment_score)::float AS avg_score, "
                    "COUNT(*)::int AS cnt, "
                    "COUNT(*) FILTER (WHERE impact_level = 'high')::int AS high_impact_count, "
                    "MAX(analyzed_at) AS latest "
                    "FROM news_events "
                    "WHERE ticker = ANY(:tickers) AND analyzed_at >= :since "
                    "GROUP BY ticker"
                ),
                {"tickers": list(universe), "since": since},
            )
            agg_rows = res.mappings().all()

            # event_type 분포: (ticker, event_type) → count.
            res = await conn.execute(
                text(
                    "SELECT ticker, event_type, COUNT(*)::int AS cnt "
                    "FROM news_events "
                    "WHERE ticker = ANY(:tickers) AND analyzed_at >= :since "
                    "GROUP BY ticker, event_type"
                ),
                {"tickers": list(universe), "since": since},
            )
            type_rows = res.mappings().all()

        event_types_by_ticker: dict[str, dict[str, int]] = {}
        for r in type_rows:
            event_types_by_ticker.setdefault(r["ticker"], {})[r["event_type"]] = int(r["cnt"])

        by_ticker: dict[str, NewsScoreEntry] = {}
        for r in agg_rows:
            latest: datetime = r["latest"]
            staleness = max(
                0.0,
                (
                    now - latest.replace(tzinfo=None) if latest.tzinfo else now - latest
                ).total_seconds()
                / 3600.0,
            )
            by_ticker[r["ticker"]] = NewsScoreEntry(
                score=_clamp(float(r["avg_score"]), -1.0, 1.0),
                timestamp=latest,
                article_count=int(r["cnt"]),
                staleness_hours=staleness,
                high_impact_count=int(r.get("high_impact_count") or 0),
                event_types=event_types_by_ticker.get(r["ticker"], {}),
            )

        # 기사 0건 ticker 를 zero entry 로 채움 (NewsPipelineScoutFeeder 컨벤션).
        out: dict[str, NewsScoreEntry] = {}
        for ticker in universe:
            if ticker in by_ticker:
                out[ticker] = by_ticker[ticker]
            else:
                out[ticker] = NewsScoreEntry(
                    score=0.0,
                    timestamp=now,
                    article_count=0,
                    staleness_hours=self._lookback_hours,
                )
        return out


def _clamp(x: float, lo: float, hi: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- SectorMomentum


class RealSectorMomentumFeeder:
    """섹터별 ~20 영업일 평균 수익률 (daily_prices 비면 {} + WARNING)."""

    def __init__(
        self,
        engine: AsyncEngine,
        lookback_days: int = SECTOR_MOMENTUM_LOOKBACK_DAYS,
    ) -> None:
        self._engine = engine
        self._lookback_days = lookback_days

    async def fetch(self, as_of: date) -> dict[str, float]:
        async with self._engine.begin() as conn:
            count_res = await conn.execute(text("SELECT COUNT(*) FROM daily_prices"))
            total = count_res.scalar_one()
            if not total:
                logger.warning(
                    "daily_prices 가 비어있음 — sector_momentum 빈 dict 반환 (as_of=%s)",
                    as_of,
                )
                return {}

            # 최신 장일 (as_of 이하) 및 ~20 영업일 이전 시작일 결정.
            latest_res = await conn.execute(
                text("SELECT MAX(price_date) FROM daily_prices WHERE price_date <= :t"),
                {"t": as_of},
            )
            end_date = latest_res.scalar_one_or_none()
            if end_date is None:
                logger.warning(
                    "daily_prices 에 as_of (%s) 이하 데이터 없음 — sector_momentum {} 반환",
                    as_of,
                )
                return {}

            fallback_since = end_date - timedelta(days=SECTOR_MOMENTUM_FALLBACK_WINDOW_DAYS)
            start_res = await conn.execute(
                text(
                    "SELECT MIN(price_date) FROM daily_prices "
                    "WHERE price_date >= :since AND price_date <= :end"
                ),
                {"since": fallback_since, "end": end_date},
            )
            start_date = start_res.scalar_one_or_none()
            if start_date is None or start_date == end_date:
                logger.warning(
                    "daily_prices window 부족 (start=%s end=%s) — {} 반환",
                    start_date,
                    end_date,
                )
                return {}

            # 섹터별: 종목별로 (end_close - start_close) / start_close 를 구한 뒤 평균.
            res = await conn.execute(
                text(
                    "SELECT sm.sector_group AS sector, "
                    "AVG((dp_end.close_price - dp_start.close_price)::float "
                    "    / NULLIF(dp_start.close_price, 0)) AS momentum "
                    "FROM stock_masters sm "
                    "JOIN daily_prices dp_end "
                    "  ON sm.stock_code = dp_end.stock_code "
                    "  AND dp_end.price_date = :end "
                    "JOIN daily_prices dp_start "
                    "  ON sm.stock_code = dp_start.stock_code "
                    "  AND dp_start.price_date = :start "
                    "WHERE sm.sector_group IS NOT NULL "
                    "GROUP BY sm.sector_group"
                ),
                {"start": start_date, "end": end_date},
            )
            rows = res.mappings().all()

        out: dict[str, float] = {}
        for r in rows:
            mom = r["momentum"]
            if mom is None:
                continue
            out[r["sector"]] = float(mom)
        return out


# ---------------------------------------------------------------- MarketSummary


class RealMarketSummaryFeeder:
    """index_daily_prices + daily_prices 로 MarketSummary 구성."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def fetch(self, as_of: date) -> MarketSummary:
        async with self._engine.begin() as conn:
            kospi_close, kospi_pct = await self._latest_index(conn, "KOSPI")
            kosdaq_close, kosdaq_pct = await self._latest_index(conn, "KOSDAQ")

            count_res = await conn.execute(text("SELECT COUNT(*) FROM daily_prices"))
            total = count_res.scalar_one()
            if not total:
                logger.warning(
                    "daily_prices 비어있음 — up/down_count=0 placeholder (as_of=%s)", as_of
                )
                up_count = 0
                down_count = 0
            else:
                up_count, down_count = await self._up_down(conn, as_of)

        return MarketSummary(
            kospi_close=kospi_close,
            kospi_change_pct=kospi_pct,
            kosdaq_close=kosdaq_close,
            kosdaq_change_pct=kosdaq_pct,
            up_count=up_count,
            down_count=down_count,
        )

    async def _latest_index(self, conn, code: str) -> tuple[float, float]:
        res = await conn.execute(
            text(
                "SELECT close_price, change_pct FROM index_daily_prices "
                "WHERE index_code = :c ORDER BY price_date DESC LIMIT 1"
            ),
            {"c": code},
        )
        row = res.mappings().one_or_none()
        if row is None:
            logger.warning("index_daily_prices 에 %s 없음 — 0/0 fallback", code)
            return 0.0, 0.0
        close = float(row["close_price"])
        pct_raw = row["change_pct"]
        # DB 는 percent (예: 1.23 = 1.23%), schema 는 소수 (0.0123) 이므로 /100.
        pct = float(pct_raw) / 100.0 if pct_raw is not None else 0.0
        return close, pct

    async def _up_down(self, conn, as_of: date) -> tuple[int, int]:
        latest_res = await conn.execute(
            text("SELECT MAX(price_date) FROM daily_prices WHERE price_date <= :t"),
            {"t": as_of},
        )
        latest = latest_res.scalar_one_or_none()
        if latest is None:
            return 0, 0
        res = await conn.execute(
            text(
                "SELECT "
                "SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END)::int AS up_count, "
                "SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END)::int AS down_count "
                "FROM daily_prices "
                "WHERE price_date = :d AND change_pct IS NOT NULL"
            ),
            {"d": latest},
        )
        row = res.mappings().one_or_none()
        if row is None:
            return 0, 0
        return int(row["up_count"] or 0), int(row["down_count"] or 0)
