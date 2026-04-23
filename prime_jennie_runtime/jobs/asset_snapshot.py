"""일일 자산 스냅샷 job.

v2 원본: `/jobs/daily-asset-snapshot` (services/jobs/app.py:87-163).

v3 어댑터:
- KIS 직접 호출 → kis_gateway HTTP `/api/balance` (PortfolioState 응답).
- v2 trade_logs (SQLModel, 직접 INSERT) → v3 outcomes (sheet 단위 closed PnL).
  → "오늘의 실현 손익" 의미는 같으나 sheet 가 닫힌 시점 (closed_at::date = today) 기준.
- daily_asset_snapshots UPSERT 는 PK=snapshot_date.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 장 마감 직후(15:30~16:00)에 KIS gateway 가 일시 멈춤/재시작을 겪으면
# ConnectError 로 떨어지는 케이스가 반복됨 (4-20 ~ 4-23 관측).
# 3회 exponential backoff 로 internal 일시장애 흡수.
_RETRY_WAITS: tuple[float, ...] = (5.0, 15.0)
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


async def _fetch_balance_with_retry(
    http: httpx.AsyncClient,
    gateway_url: str,
) -> dict:
    """장 마감 직후 kis-gateway 의 일시 장애를 견디는 retry wrapper."""
    last_exc: Exception | None = None
    attempts = len(_RETRY_WAITS) + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = await http.get(f"{gateway_url}/api/balance", timeout=15.0)
            resp.raise_for_status()
            return resp.json()
        except _RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt >= attempts:
                break
            wait = _RETRY_WAITS[attempt - 1]
            logger.warning(
                "kis-gateway balance 조회 실패 (%d/%d) %s — %.0fs 재시도",
                attempt,
                attempts,
                type(e).__name__,
                wait,
            )
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def daily_asset_snapshot(
    pool: Any,
    http: httpx.AsyncClient,
    gateway_url: str,
) -> None:
    """v2 `/jobs/daily-asset-snapshot` 포팅.

    1) KIS gateway `/api/balance` → total_asset / cash / stock_eval / positions
       (장 마감 직후 일시 장애 대응으로 3회 retry)
    2) 미실현 PnL = sum(current_value - total_buy_amount) over positions
    3) 실현 PnL = sum(outcomes.pnl_krw WHERE closed_at::date = today)
    4) daily_asset_snapshots UPSERT (PK=snapshot_date)
    """
    balance = await _fetch_balance_with_retry(http, gateway_url)

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
