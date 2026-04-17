"""일일 자산 스냅샷 job.

v2 원본: `/jobs/daily-asset-snapshot` (services/jobs/app.py:87-163).

v3 어댑터:
- KIS 직접 호출 → kis_gateway HTTP `/api/balance` (PortfolioState 응답).
- v2 trade_logs (SQLModel, 직접 INSERT) → v3 outcomes (sheet 단위 closed PnL).
  → "오늘의 실현 손익" 의미는 같으나 sheet 가 닫힌 시점 (closed_at::date = today) 기준.
- daily_asset_snapshots UPSERT 는 PK=snapshot_date.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def daily_asset_snapshot(
    pool: Any,
    http: httpx.AsyncClient,
    gateway_url: str,
) -> None:
    """v2 `/jobs/daily-asset-snapshot` 포팅.

    1) KIS gateway `/api/balance` → total_asset / cash / stock_eval / positions
    2) 미실현 PnL = sum(current_value - total_buy_amount) over positions
    3) 실현 PnL = sum(outcomes.pnl_krw WHERE closed_at::date = today)
    4) daily_asset_snapshots UPSERT (PK=snapshot_date)
    """
    resp = await http.get(f"{gateway_url}/api/balance", timeout=15.0)
    resp.raise_for_status()
    balance = resp.json()

    positions = balance.get("positions", []) or []
    total = int(balance.get("total_asset") or 0)
    cash = int(balance.get("cash_balance") or 0)
    stock_eval = int(balance.get("stock_eval_amount") or 0)
    if total <= 0:  # v2 와 동일한 fallback
        total = cash + stock_eval

    unrealized_pnl = 0
    for p in positions:
        current_val = int(p.get("current_value") or 0)
        buy_amt = int(p.get("total_buy_amount") or 0)
        unrealized_pnl += current_val - buy_amt

    today = date.today()

    async with pool.acquire() as conn:
        realized_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(pnl_krw), 0)::BIGINT AS realized "
            "FROM outcomes "
            "WHERE closed_at::date = $1",
            today,
        )
        realized_pnl = int(realized_row["realized"]) if realized_row else 0
        total_pnl = unrealized_pnl + realized_pnl

        await conn.execute(
            "INSERT INTO daily_asset_snapshots "
            "(snapshot_date, total_asset, cash_balance, stock_eval_amount, "
            "position_count, total_profit_loss, realized_profit_loss) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (snapshot_date) DO UPDATE SET "
            "total_asset = EXCLUDED.total_asset, "
            "cash_balance = EXCLUDED.cash_balance, "
            "stock_eval_amount = EXCLUDED.stock_eval_amount, "
            "position_count = EXCLUDED.position_count, "
            "total_profit_loss = EXCLUDED.total_profit_loss, "
            "realized_profit_loss = EXCLUDED.realized_profit_loss",
            today,
            total,
            cash,
            stock_eval,
            len(positions),
            total_pnl,
            realized_pnl,
        )

    logger.info(
        "daily_asset_snapshot: total=%s unrealized=%s realized=%s positions=%d",
        f"{total:,}",
        f"{unrealized_pnl:,}",
        f"{realized_pnl:,}",
        len(positions),
    )


__all__ = ["daily_asset_snapshot"]
