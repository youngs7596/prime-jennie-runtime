"""ScoutContext 조립 유틸.

feeder들을 호출해 ScoutContext를 합친다. 역순으로 주입만 교체하면
stub → real feeder 전환 가능.

2026-05-15 audit B1 fix — 거래 이력 노출:
  - today_entries: 같은 거래일 이미 sheet 발행된 ticker
  - recent_stop_loss_tickers: 24h 내 손절 발생한 ticker
  pool 주입 시 DB 에서 조회. 미주입 / 조회 실패 시 빈 list (fail-open).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from prime_jennie_runtime.position_sheet.schema import ALLOWED_STRATEGY_TAGS

from .feeders.base import (
    ConsensusFeeder,
    MarketSummaryFeeder,
    NewsEventFeeder,
    SectorMomentumFeeder,
    UniverseFeeder,
)
from .schemas import MacroStateForScout, ScoutContext, ScoutRunSummary

logger = logging.getLogger(__name__)

# 24h 내 stop sell 검사 reason 목록 — fast_loop/cooldown_check.py 와 동일.
_STOP_REASONS = ("fixed_sl", "stop_loss", "breakeven_stop")

_SQL_TODAY_ENTRIES = (
    "SELECT DISTINCT ticker FROM position_sheets "
    "WHERE (generated_at AT TIME ZONE 'Asia/Seoul')::date = "
    "      (now() AT TIME ZONE 'Asia/Seoul')::date "
    "ORDER BY ticker"
)
# SQLAlchemy named param 형식 — :reasons 는 list[str] bind.
# 2026-05-15 emergency fix: persistence.py:143 키는 'exit_reason'.
_SQL_RECENT_STOPS_SA = (
    "SELECT DISTINCT ps.ticker "
    "FROM executions e JOIN position_sheets ps USING (sheet_id) "
    "WHERE e.side = 'sell' "
    "  AND (e.metadata_json->>'exit_reason') = ANY(:reasons) "
    "  AND e.executed_at > now() - interval '24 hours' "
    "ORDER BY ps.ticker"
)


async def _fetch_today_entries(engine: Any | None) -> list[str]:
    """SQLAlchemy AsyncEngine 사용 — slow_loop 의 feeder 와 일관성."""
    if engine is None:
        return []
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            result = await conn.execute(text(_SQL_TODAY_ENTRIES))
            return [row[0] for row in result.fetchall()]
    except Exception:
        logger.exception("today_entries fetch failed — using empty list (fail-open)")
        return []


async def _fetch_recent_stops(engine: Any | None) -> list[str]:
    if engine is None:
        return []
    try:
        from sqlalchemy import text

        sql = text(_SQL_RECENT_STOPS_SA)
        async with engine.connect() as conn:
            result = await conn.execute(sql, {"reasons": list(_STOP_REASONS)})
            return [row[0] for row in result.fetchall()]
    except Exception:
        logger.exception("recent_stops fetch failed — using empty list (fail-open)")
        return []


@dataclass
class ScoutContextBuilder:
    """ScoutContext 조립기."""

    universe: UniverseFeeder
    news: NewsEventFeeder
    sector: SectorMomentumFeeder
    market: MarketSummaryFeeder
    consensus: ConsensusFeeder | None = None  # None 이면 consensus_data={}
    # SQLAlchemy AsyncEngine — None 이면 history 빈 리스트 (fail-open).
    engine: Any | None = None

    async def build(
        self,
        as_of: date,
        macro_state: MacroStateForScout,
        previous_runs: list[ScoutRunSummary] | None = None,
        trigger_reason: str = "scheduled_0830",
    ) -> ScoutContext:
        universe = await self.universe.fetch(as_of)
        news = await self.news.fetch(as_of, universe)
        sector = await self.sector.fetch(as_of)
        market = await self.market.fetch(as_of)

        consensus_data = {}
        if self.consensus is not None and universe:
            try:
                consensus_data = await self.consensus.fetch(as_of, universe)
            except Exception:
                # 후속 데이터 의존성 — fetch 실패는 fail-open 으로 빈 dict.
                logger.exception("ConsensusFeeder.fetch failed — empty consensus_data 로 진행")
                consensus_data = {}

        # audit B1 fix — 거래 이력 노출 (engine 주입 시).
        today_entries = await _fetch_today_entries(self.engine)
        recent_stops = await _fetch_recent_stops(self.engine)

        return ScoutContext(
            as_of=as_of,
            universe=universe,
            market_summary=market,
            macro_state=macro_state,
            news_events=news,
            sector_momentum=sector,
            previous_scout_runs=list(previous_runs or []),
            strategy_tags_available=sorted(ALLOWED_STRATEGY_TAGS),
            trigger_reason=trigger_reason,
            consensus_data=consensus_data,
            today_entries=today_entries,
            recent_stop_loss_tickers=recent_stops,
        )
