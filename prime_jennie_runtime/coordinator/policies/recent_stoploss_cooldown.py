"""Policy C — recent_stoploss_cooldown (advisory).

손절 24h 내 같은 종목 sheet 발행 또는 entry 시도가 감지되면 advisory_not_ok.

fast_loop 의 enforcement (``fast_loop/cooldown_check.py``) 와 같은 logic.
두 layer 가 동시에 발화해야 일관. drift 검출은 일별 batch (별도 작업).

이 정책은 차단 권한 없음 — Stage 3 (enforce mode) 진입 시 fast_loop layer 와
선택적으로 통합. 현 단계 (Stage 2) 에선 alert + decision_log 만.

design: .ai/designs/2026-05-15-cooldown-and-duplicate-guard.md §4
"""

from __future__ import annotations

from typing import Any

from prime_jennie_runtime.coordinator.events import (
    CoordinatorEvent,
    EntryQueuedEvent,
    SheetPublishedEvent,
)
from prime_jennie_runtime.coordinator.policies.base import Policy, PolicyResult

DEFAULT_WINDOW_HOURS = 24
STOP_REASONS = ("fixed_sl", "stop_loss", "breakeven_stop")

_SQL = (
    "SELECT e.executed_at, e.metadata_json->>'reason' AS reason "
    "FROM executions e "
    "JOIN position_sheets ps USING (sheet_id) "
    "WHERE ps.ticker = $1 "
    "  AND e.side = 'sell' "
    "  AND (e.metadata_json->>'reason') = ANY($2) "
    "  AND e.executed_at > now() - ($3::int * interval '1 hour') "
    "ORDER BY e.executed_at DESC "
    "LIMIT 1"
)


class RecentStoplossCooldownPolicy(Policy):
    """sheet_published / entry_queued 시 종목의 최근 손절 이력 검사."""

    name = "RecentStoplossCooldownPolicy"

    def __init__(
        self,
        *,
        window_hours: int = DEFAULT_WINDOW_HOURS,
        stop_reasons: tuple[str, ...] = STOP_REASONS,
    ) -> None:
        self._window_hours = window_hours
        self._stop_reasons = list(stop_reasons)

    def applies_to(self, event: CoordinatorEvent) -> bool:
        return isinstance(event, SheetPublishedEvent | EntryQueuedEvent)

    async def evaluate(self, pool: Any, event: CoordinatorEvent) -> PolicyResult | None:
        ticker = getattr(event, "ticker", None)
        sheet_id = getattr(event, "sheet_id", None)
        if not ticker:
            return None

        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL, ticker, self._stop_reasons, self._window_hours)
        if row is None:
            return None

        return PolicyResult(
            outcome="advisory_not_ok",
            reason="recent_stoploss_cooldown",
            policy_name=self.name,
            subject_id=sheet_id,
            context={
                "ticker": ticker,
                "last_exit_at": row["executed_at"].isoformat(),
                "last_exit_reason": row["reason"],
                "window_hours": self._window_hours,
                "trigger_event_kind": event.event_kind,
            },
        )
