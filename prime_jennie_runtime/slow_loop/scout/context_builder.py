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
from .schemas import MacroStateForScout, PreviousOutcome, ScoutContext, ScoutRunSummary

logger = logging.getLogger(__name__)

# 24h 내 stop sell 검사 reason 목록 — fast_loop/cooldown_check.py 와 동일.
_STOP_REASONS = ("fixed_sl", "stop_loss", "breakeven_stop")

_SQL_TODAY_ENTRIES = (
    "SELECT DISTINCT ticker FROM position_sheets "
    "WHERE (generated_at AT TIME ZONE 'Asia/Seoul')::date = "
    "      (now() AT TIME ZONE 'Asia/Seoul')::date "
    "ORDER BY ticker"
)

# today_exit_cooldown (2026-05-15) — exit_reason 무관, KST 거래일 sell 있는 ticker.
_SQL_TODAY_EXITS_SA = (
    "SELECT DISTINCT ps.ticker "
    "FROM executions e JOIN position_sheets ps USING (sheet_id) "
    "WHERE e.side = 'sell' "
    "  AND (e.executed_at AT TIME ZONE 'Asia/Seoul')::date = "
    "      (now() AT TIME ZONE 'Asia/Seoul')::date "
    "ORDER BY ps.ticker"
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

# G1 outcome 피드백 (2026-05-15) — scout_outcomes_v1 view 에서 최근 N일 청산 완료
# row. limit 으로 안전 가드 (LLM context 폭주 방지).
_DEFAULT_OUTCOMES_DAYS = 7
_DEFAULT_OUTCOMES_LIMIT = 50

# asyncpg 는 :days::int * interval 패턴 syntax error → make_interval 사용.
_SQL_PREVIOUS_OUTCOMES = (
    "SELECT ticker, strategy_tag, generated_at, exit_at, exit_reason, pnl_pct "
    "FROM scout_outcomes_v1 "
    "WHERE exit_at IS NOT NULL "
    "  AND exit_at > now() - make_interval(days => :days) "
    "ORDER BY exit_at DESC "
    "LIMIT :max_rows"
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


async def _fetch_today_exits(engine: Any | None) -> list[str]:
    """같은 KST 거래일 청산 ticker (exit_reason 무관). fail-open."""
    if engine is None:
        return []
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            result = await conn.execute(text(_SQL_TODAY_EXITS_SA))
            return [row[0] for row in result.fetchall()]
    except Exception:
        logger.exception("today_exits fetch failed — using empty list (fail-open)")
        return []


async def _fetch_previous_outcomes(
    engine: Any | None,
    *,
    days: int = _DEFAULT_OUTCOMES_DAYS,
    max_rows: int = _DEFAULT_OUTCOMES_LIMIT,
) -> list[PreviousOutcome]:
    """scout_outcomes_v1 view 에서 최근 N일 청산 완료 outcomes 추출 (G1).

    View 가 없거나 (migration 미적용) DB 장애 시 빈 list 반환 (fail-open) —
    Scout 의사결정에 영향 없음.
    """
    if engine is None:
        return []
    try:
        from sqlalchemy import text

        sql = text(_SQL_PREVIOUS_OUTCOMES)
        async with engine.connect() as conn:
            result = await conn.execute(sql, {"days": days, "max_rows": max_rows})
            rows = result.mappings().all()
        return [
            PreviousOutcome(
                ticker=row["ticker"],
                strategy_tag=row["strategy_tag"],
                generated_at=row["generated_at"],
                exit_at=row["exit_at"],
                exit_reason=row["exit_reason"],
                pnl_pct=float(row["pnl_pct"]) if row["pnl_pct"] is not None else None,
            )
            for row in rows
        ]
    except Exception:
        logger.exception("previous_outcomes fetch failed — using empty list (fail-open)")
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
        # today_exit_cooldown — 같은 거래일 청산 ticker (exit_reason 무관).
        today_exits = await _fetch_today_exits(self.engine)
        # G1 — outcome 피드백 (engine 주입 + view 존재 시).
        previous_outcomes = await _fetch_previous_outcomes(self.engine)

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
            recently_exited_today_tickers=today_exits,
            previous_outcomes=previous_outcomes,
        )
