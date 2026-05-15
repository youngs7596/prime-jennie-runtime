"""Recent stop-loss cooldown — 손절 직후 동일 종목 재진입 차단.

trading-domain.md:48 의 v2 "Cooldown 3일 + 24h" 룰이 v3 미포팅이었음.
2026-05-15 시초에 어제 손절한 001440 / 062040 을 09:30 sheet 가 재발행하고
fast_loop 가 그대로 entry → 같은 날 재진입 발생. 본 모듈이 그 게이트를 복구.

설계:
  - PositionSheetConsumer 가 sheet 처리 직전 ``has_recent_stoploss(ticker)``
    호출. 결과가 있으면 enqueue skip + entry_rejected event 발행.
  - Protocol 기반 — 테스트는 in-memory fake 로, 운영은 PgCooldownChecker.
  - 데이터 truth source: ``executions`` 테이블 (FK 로 ``position_sheets.ticker``
    join). redis cache 미사용 — redis flush / 재시작에 강해야 함.

범위 / 정책:
  - **종목 단위** (strategy_tag 무관). 같은 종목 24h 내 손절 직후엔 어떤
    전략이든 재진입 보수.
  - **24h window** (trading-domain.md 의 v2 24h 룰). 운영 1주 누적 후 조정 후보.
  - **stop reason whitelist**: ``fixed_sl``, ``stop_loss``, ``breakeven_stop``.
    breakeven 은 -0% 손익이라 보수 불필요해 보이지만 stop trigger 가 동일하므로
    포함. user_manual / strategy_exit (전략 종료) 는 제외 (자연 청산).

상위 design: ``.ai/designs/2026-05-15-cooldown-and-duplicate-guard.md`` §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncpg

DEFAULT_WINDOW_HOURS = 24
STOP_REASONS = ("fixed_sl", "stop_loss", "breakeven_stop")


@dataclass(frozen=True)
class RecentStopLoss:
    """24h 내 손절 1건 요약 — log/event 에 노출."""

    ticker: str
    last_exit_at: datetime
    reason: str


class CooldownChecker(Protocol):
    """fast_loop consumer 가 의존하는 추상 인터페이스."""

    async def has_recent_stoploss(self, ticker: str) -> RecentStopLoss | None: ...


class NullCooldownChecker:
    """항상 None — 미주입 / 테스트용 fallback."""

    async def has_recent_stoploss(self, ticker: str) -> RecentStopLoss | None:  # noqa: ARG002
        return None


class PgCooldownChecker:
    """asyncpg pool 기반 — 운영 구현체.

    Parameters
    ----------
    pool:
        fast_loop app.py 에서 생성한 asyncpg pool.
    window_hours:
        손절 이력 검사 window. 기본 24.
    stop_reasons:
        cooldown trigger 되는 metadata reason 목록.
    """

    # 2026-05-15 emergency fix: persistence.py:143 는 'exit_reason' 키로 저장
    # 하는데 본 SQL 이 'reason' 키를 보고 있어 가드가 항상 false negative.
    # row 별칭은 RecentStopLoss.reason 필드와 contract 유지를 위해 'reason' 으로 둠.
    _SQL = (
        "SELECT e.executed_at, e.metadata_json->>'exit_reason' AS reason "
        "FROM executions e "
        "JOIN position_sheets ps USING (sheet_id) "
        "WHERE ps.ticker = $1 "
        "  AND e.side = 'sell' "
        "  AND (e.metadata_json->>'exit_reason') = ANY($2) "
        "  AND e.executed_at > now() - ($3::int * interval '1 hour') "
        "ORDER BY e.executed_at DESC "
        "LIMIT 1"
    )

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        window_hours: int = DEFAULT_WINDOW_HOURS,
        stop_reasons: tuple[str, ...] = STOP_REASONS,
    ) -> None:
        self._pool = pool
        self._window_hours = window_hours
        self._stop_reasons = list(stop_reasons)

    async def has_recent_stoploss(self, ticker: str) -> RecentStopLoss | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._SQL, ticker, self._stop_reasons, self._window_hours)
        if row is None:
            return None
        return RecentStopLoss(
            ticker=ticker,
            last_exit_at=row["executed_at"],
            reason=row["reason"],
        )
