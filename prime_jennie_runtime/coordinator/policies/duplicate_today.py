"""Policy B2 — duplicate_today (advisory).

같은 거래일 (당일 KST) 에 같은 ticker 의 sheet 가 이미 발행되어 있으면
advisory_not_ok. audit B2 의 ``ActiveSheetChecker`` dead code 정확히 대응.

trigger: sheet_published 이벤트만. entry_queued 는 자기 자신이 이미 sheet
1건이므로 검사 무의미.

design: .ai/designs/2026-05-15-cooldown-and-duplicate-guard.md §4
"""

from __future__ import annotations

from typing import Any

from prime_jennie_runtime.coordinator.events import (
    CoordinatorEvent,
    SheetPublishedEvent,
)
from prime_jennie_runtime.coordinator.policies.base import Policy, PolicyResult

# 본인 자신을 제외해야 하므로 sheet_id != $2 조건. KST 기준 거래일 시작
# (00:00 KST = 전날 15:00 UTC) 의 timestamp 비교는 generated_at::date 로
# 처리 — postgres 의 default timezone 이 UTC 라면 KST 변환 후 date.
_SQL = (
    "SELECT count(*)::int AS n "
    "FROM position_sheets "
    "WHERE ticker = $1 "
    "  AND sheet_id <> $2 "
    "  AND (generated_at AT TIME ZONE 'Asia/Seoul')::date "
    "      = (now() AT TIME ZONE 'Asia/Seoul')::date"
)


class DuplicateTodayPolicy(Policy):
    """같은 거래일 같은 ticker 재발행 검사."""

    name = "DuplicateTodayPolicy"

    def applies_to(self, event: CoordinatorEvent) -> bool:
        return isinstance(event, SheetPublishedEvent)

    async def evaluate(self, pool: Any, event: CoordinatorEvent) -> PolicyResult | None:
        ticker = getattr(event, "ticker", None)
        sheet_id = getattr(event, "sheet_id", None)
        if not ticker or not sheet_id:
            return None

        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SQL, ticker, sheet_id)
        n = (row["n"] if row else 0) or 0
        if n <= 0:
            return None

        return PolicyResult(
            outcome="advisory_not_ok",
            reason="duplicate_today",
            policy_name=self.name,
            subject_id=sheet_id,
            context={
                "ticker": ticker,
                "earlier_sheets_today": n,
            },
        )
