"""ScoutContext 조립 유틸.

feeder들을 호출해 ScoutContext를 합친다. 역순으로 주입만 교체하면
stub → real feeder 전환 가능.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

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


@dataclass
class ScoutContextBuilder:
    """ScoutContext 조립기."""

    universe: UniverseFeeder
    news: NewsEventFeeder
    sector: SectorMomentumFeeder
    market: MarketSummaryFeeder
    consensus: ConsensusFeeder | None = None  # None 이면 consensus_data={}

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
        )
